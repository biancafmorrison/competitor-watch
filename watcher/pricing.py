"""Pricing-page change tracking: snapshot prices + plan headings, diff against last snapshot."""
import hashlib
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from . import db
from .fetch import FetchError, normalize_url

PRICE_RE = re.compile(
    r"(?:US\$|CA\$|A\$|\$|€|£|₹|USD\s?|EUR\s?|GBP\s?|INR\s?|Rs\.?\s?)\s?\d[\d,]*(?:\.\d{1,2})?"
    r"(?:\s?(?:/|per\s)\s?(?:mo(?:nth)?|yr|year|user|seat|member|editor|agent)\b)?", re.I)
PRICING_SEGS = {"pricing", "plans", "price", "prices", "plans-pricing", "plans-and-pricing", "pricing-plans"}
PRICING_TEXT = {"pricing", "plans", "plans & pricing", "plans and pricing", "see pricing", "view pricing"}


def _candidates(fetcher, base, home_links):
    out = []
    for url, text, _a in home_links:
        p = urlparse(url)
        if not fetcher.in_scope(p.hostname):
            continue
        segs = [s.lower() for s in p.path.split("/") if s]
        if (segs and segs[-1] in PRICING_SEGS and len(segs) <= 2) or text.strip().lower() in PRICING_TEXT:
            out.append(normalize_url(url))
    linked = list(dict.fromkeys(out))
    defaults = [normalize_url(urljoin(base, p)) for p in ("/pricing", "/plans", "/pricing/plans")]
    return linked, [d for d in dict.fromkeys(defaults) if d not in linked]


def _extract(content):
    soup = BeautifulSoup(content, "lxml")
    for t in soup(["script", "style", "noscript", "nav", "footer", "header", "svg"]):
        t.decompose()
    text = soup.get_text(" ", strip=True)
    prices = sorted({re.sub(r"\s+", " ", m.group(0)).strip().lower() for m in PRICE_RE.finditer(text)})
    heads = []
    for h in soup.find_all(["h1", "h2", "h3"]):
        t = re.sub(r"\s+", " ", h.get_text(" ", strip=True))
        if 1 < len(t) <= 80 and t not in heads:
            heads.append(t)
    return prices, heads[:40], bool(re.search(r"pric|plan|per month|/mo|free trial", text[:6000], re.I))


def _diff(prev_list, cur_list):
    a, b = set(prev_list), set(cur_list)
    return sorted(b - a), sorted(a - b)


def check(fetcher, base, home_links, domain_id):
    linked, defaults = _candidates(fetcher, base, home_links)
    found = None
    for url in (linked + defaults)[:5]:
        try:
            r = fetcher.get(url)
        except FetchError:
            continue
        prices, heads, looks = _extract(r.content)
        if prices or (url in linked and looks):
            found = (getattr(r, "final_url", url), prices, heads)
            break
    if not found:
        return {"status": "not_found", "note": "No pricing page with visible prices was found "
                "(it may be rendered by JavaScript, hidden behind a demo form, or not exist)."}
    url, prices, heads = found
    digest = hashlib.sha256(json.dumps([prices, heads]).encode()).hexdigest()
    prev = db.one("SELECT * FROM pricing_snapshots WHERE domain_id=? ORDER BY id DESC LIMIT 1", (domain_id,))
    snap = lambda: db.execute(
        "INSERT INTO pricing_snapshots(domain_id,url,taken_at,content_hash,prices_json,headings_json) VALUES (?,?,?,?,?,?)",
        (domain_id, url, db.now(), digest, db.dumps(prices), db.dumps(heads)))
    if not prev:
        snap()
        return {"status": "baseline", "url": url, "prices": prices,
                "note": f"Baseline saved ({len(prices)} price points). Changes will be flagged on later runs."}
    if prev["content_hash"] == digest:
        return {"status": "unchanged", "url": url, "prices": prices}
    p_prices, p_heads = json.loads(prev["prices_json"]), json.loads(prev["headings_json"])
    add_p, rem_p = _diff(p_prices, prices)
    add_h, rem_h = _diff(p_heads, heads)
    snap()
    if not add_p and not rem_p:
        # only headings/copy moved: record silently, don't alert
        return {"status": "unchanged", "url": url, "prices": prices,
                "note": "Page copy changed but no price points did."}
    return {"status": "changed", "url": url, "prices": prices,
            "added_prices": add_p, "removed_prices": rem_p,
            "added_headings": add_h[:10], "removed_headings": rem_h[:10]}
