"""Target-keyword extraction (heuristic) + search-volume enrichment via a free/freemium API."""
import hashlib
import re
from urllib.parse import urlparse

import requests

from .config import cfg

STOP = {"a", "an", "the", "of", "to", "for", "and", "or", "in", "on", "with", "your", "you", "our", "we",
        "is", "are", "be", "why", "what's", "this", "that", "these", "from", "by", "at", "as", "it", "its",
        "ultimate", "complete", "definitive", "comprehensive", "essential", "step-by-step", "guide", "tips",
        "best", "top"}
KEEP_LEAD = ("how to", "what is", "what are", "best ", "top ", "vs ", " vs ")
FLUFF = re.compile(r"\b(ultimate|complete|definitive|comprehensive|beginner'?s?|step[- ]by[- ]step|"
                   r"in 20\d\d|20\d\d|updated|\d+\s+(?:best|top|ways|tips|examples|tools|ideas|reasons)?)\b", re.I)


class KeywordError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind, self.message = kind, message   # auth | quota | rate | network | error | not_configured


def _tokens(s):
    return re.findall(r"[a-z0-9']+", s.lower())


def pick_keyword(title, meta_keywords, url):
    """Best-guess target keyword: meta keywords > URL slug (if it matches the title) > cleaned title."""
    if meta_keywords:
        k = meta_keywords[0].strip().lower()
        if 1 <= len(k.split()) <= 6:
            return k
    t_tokens = set(_tokens(title or ""))
    slug = [s for s in urlparse(url).path.split("/") if s]
    slug = slug[-1] if slug else ""
    slug = re.sub(r"\.(html?|php|aspx?)$", "", slug)
    words = [w for w in re.split(r"[-_]+", slug.lower()) if w and not re.fullmatch(r"\d{4}|\d+", w)]
    if 2 <= len(words) <= 7 and t_tokens and sum(w in t_tokens for w in words) / len(words) >= 0.7:
        return " ".join(words)
    if not title:
        return " ".join(words[:6]) if words else ""
    seg = re.split(r"[:|\u2013\u2014?]|\s-\s", title)[0] if len(title) > 40 else title
    seg = FLUFF.sub(" ", seg)
    toks = _tokens(seg)
    while toks and toks[0] in STOP and not " ".join(toks).startswith(KEEP_LEAD):
        toks.pop(0)
    while toks and toks[-1] in STOP:
        toks.pop()
    return " ".join(toks[:6])


def _num(x, cast=float):
    try:
        return cast(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- providers
def _keywords_everywhere(kws):
    key = __import__("os").getenv("KEYWORDS_EVERYWHERE_API_KEY", "").strip()
    if not key:
        raise KeywordError("not_configured", "KEYWORDS_EVERYWHERE_API_KEY is not set")
    data = [("country", cfg.KEYWORD_COUNTRY), ("currency", "USD"), ("dataSource", "gkp")]
    data += [("kw[]", k) for k in kws]
    try:
        r = requests.post("https://api.keywordseverywhere.com/v1/get_keyword_data",
                          headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                          data=data, timeout=30)
    except requests.RequestException:
        raise KeywordError("network", "Could not reach the Keywords Everywhere API")
    if r.status_code in (401, 403):
        raise KeywordError("auth", "Keywords Everywhere rejected the API key (invalid or expired)")
    if r.status_code == 402:
        raise KeywordError("quota", "Keywords Everywhere credits exhausted")
    if r.status_code == 429:
        raise KeywordError("rate", "Keywords Everywhere rate limit hit")
    if r.status_code != 200:
        raise KeywordError("error", f"Keywords Everywhere returned HTTP {r.status_code}")
    out = {}
    for row in r.json().get("data", []):
        cpc = row.get("cpc")
        cpc = _num(cpc.get("value")) if isinstance(cpc, dict) else _num(cpc)
        out[row.get("keyword", "").lower()] = {"volume": _num(row.get("vol"), int), "cpc": cpc,
                                               "competition": _num(row.get("competition"))}
    return out


DFS_LOC = {"us": 2840, "gb": 2826, "uk": 2826, "in": 2356, "ca": 2124, "au": 2036, "de": 2276, "fr": 2250}


def _dataforseo(kws):
    import os
    login, pw = os.getenv("DATAFORSEO_LOGIN", "").strip(), os.getenv("DATAFORSEO_PASSWORD", "").strip()
    if not login or not pw:
        raise KeywordError("not_configured", "DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD are not set")
    clean = [re.sub(r"[^\w\s'-]", " ", k).strip()[:80] for k in kws]
    body = [{"keywords": clean, "location_code": DFS_LOC.get(cfg.KEYWORD_COUNTRY, 2840), "language_code": "en"}]
    try:
        r = requests.post("https://api.dataforseo.com/v3/keywords_data/google_ads/search_volume/live",
                          json=body, auth=(login, pw), timeout=60)
    except requests.RequestException:
        raise KeywordError("network", "Could not reach the DataForSEO API")
    if r.status_code in (401, 403):
        raise KeywordError("auth", "DataForSEO rejected the login/password")
    if r.status_code == 429:
        raise KeywordError("rate", "DataForSEO rate limit hit")
    if r.status_code != 200:
        raise KeywordError("error", f"DataForSEO returned HTTP {r.status_code}")
    js = r.json()
    code = js.get("status_code")
    if code in (40100, 40101, 40102):
        raise KeywordError("auth", "DataForSEO rejected the login/password")
    if code == 40200:
        raise KeywordError("quota", "DataForSEO balance/credits exhausted")
    task = (js.get("tasks") or [{}])[0]
    if task.get("status_code") not in (20000, None):
        kind = "quota" if task.get("status_code") == 40200 else "error"
        raise KeywordError(kind, f"DataForSEO: {task.get('status_message', 'task failed')}")
    out = {}
    for row in task.get("result") or []:
        ci = row.get("competition_index")
        out[str(row.get("keyword", "")).lower()] = {
            "volume": _num(row.get("search_volume"), int), "cpc": _num(row.get("cpc")),
            "competition": (ci / 100.0) if isinstance(ci, (int, float)) else None}
    # DFS lowercases/sanitises: map back to what we asked for
    return {k.lower(): out.get(re.sub(r"[^\w\s'-]", " ", k).strip().lower()) for k in kws if
            out.get(re.sub(r"[^\w\s'-]", " ", k).strip().lower())}


def _mock(kws):   # offline demo/testing only
    out = {}
    for k in kws:
        h = int(hashlib.md5(k.encode()).hexdigest()[:6], 16)
        out[k.lower()] = {"volume": (h % 90) * 100 + 100, "cpc": round((h % 500) / 100, 2),
                          "competition": round((h % 100) / 100, 2)}
    return out


PROVIDERS = {"keywordseverywhere": _keywords_everywhere, "dataforseo": _dataforseo, "mock": _mock}


def provider_name():
    return cfg.KEYWORD_PROVIDER


def lookup(keywords):
    """keywords: list[str] -> {keyword_lower: {volume,cpc,competition}}. Raises KeywordError."""
    p = provider_name()
    if p in ("", "none"):
        raise KeywordError("not_configured", "No keyword API configured (set KEYWORD_PROVIDER)")
    fn = PROVIDERS.get(p)
    if not fn:
        raise KeywordError("not_configured", f"Unknown KEYWORD_PROVIDER '{p}'")
    uniq = list(dict.fromkeys(k for k in keywords if k))
    out = {}
    for i in range(0, len(uniq), 100):
        out.update(fn(uniq[i:i + 100]))
    return out
