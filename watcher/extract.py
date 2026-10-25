"""Pull title / publish date / meta description (and article-ness) out of a page."""
import json
import re
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup
from dateutil import parser as dparser

ARTICLE_TYPES = {"article", "blogposting", "newsarticle", "techarticle", "report",
                 "scholarlyarticle", "analysisnewsarticle", "opinionnewsarticle"}

DATE_METAS = ("article:published_time", "og:article:published_time", "datepublished",
              "date", "dc.date.issued", "dc.date", "publish_date", "pubdate",
              "sailthru.date", "parsely-pub-date", "article:modified_time")


def parse_date(s):
    if not s:
        return None
    try:
        dt = dparser.parse(str(s).strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt.year < 1995 or dt > datetime.now(timezone.utc) + timedelta(days=2):
            return None
        return dt.date().isoformat()
    except (ValueError, OverflowError, TypeError):
        return None


def _meta(soup, *names):
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n}) \
              or soup.find("meta", attrs={"itemprop": n})
        if tag and tag.get("content", "").strip():
            return tag["content"].strip()
    return None


def _jsonld_nodes(soup):
    out = []
    for sc in soup.find_all("script", type=lambda t: t and "ld+json" in t):
        raw = sc.string or sc.get_text() or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                out.append(d)
                if "@graph" in d:
                    stack.append(d["@graph"])
    return out


def _is_article_node(n):
    t = n.get("@type")
    ts = t if isinstance(t, list) else [t]
    return any(isinstance(x, str) and x.lower() in ARTICLE_TYPES for x in ts)


def _clean_title(title, site_name):
    if not title:
        return title
    title = re.sub(r"\s+", " ", title).strip()
    parts = re.split(r"\s+[|\u2013\u2014\u2022\u00b7:-]\s+", title)
    if len(parts) > 1:
        last = parts[-1].strip().lower()
        if (site_name and last == site_name.strip().lower()) or len(last.split()) <= 3:
            title = " - ".join(parts[:-1]).strip() or title
    return title


def parse_article(content, url):
    """Return dict(title, published, description, meta_keywords, h1, is_article, word_count)."""
    soup = BeautifulSoup(content, "lxml")
    site_name = _meta(soup, "og:site_name")
    nodes = [n for n in _jsonld_nodes(soup) if _is_article_node(n)]
    ld = nodes[0] if nodes else {}

    h1_tag = soup.find("article")
    h1_tag = (h1_tag.find("h1") if h1_tag else None) or soup.find("h1")
    h1 = h1_tag.get_text(" ", strip=True) if h1_tag else None

    raw_title = ld.get("headline") if isinstance(ld.get("headline"), str) else None
    raw_title = raw_title or _meta(soup, "og:title", "twitter:title") or h1 or \
        (soup.title.get_text(strip=True) if soup.title else None)
    title = _clean_title(raw_title, site_name)

    published = parse_date(ld.get("datePublished"))
    if not published:
        for m in DATE_METAS:
            published = parse_date(_meta(soup, m))
            if published:
                break
    if not published:
        art = soup.find("article") or soup
        for t in art.find_all("time"):
            published = parse_date(t.get("datetime") or t.get_text(strip=True))
            if published:
                break
    if not published:
        m = re.search(r"/(20\d\d)/(\d{1,2})(?:/(\d{1,2}))?/", url)
        if m:
            published = parse_date(f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3) or 1):02d}")

    desc = _meta(soup, "description", "og:description", "twitter:description")
    if not desc and isinstance(ld.get("description"), str):
        desc = ld["description"].strip()
    if desc:
        desc = re.sub(r"\s+", " ", desc)[:500]

    mk = _meta(soup, "keywords", "news_keywords")
    meta_keywords = [k.strip() for k in mk.split(",") if k.strip()] if mk else []
    if not meta_keywords and isinstance(ld.get("keywords"), (str, list)):
        kw = ld["keywords"]
        meta_keywords = [k.strip() for k in (kw.split(",") if isinstance(kw, str) else kw) if str(k).strip()]

    body = soup.find("article") or soup.find("main") or soup.body
    words = len(body.get_text(" ", strip=True).split()) if body else 0

    og_type = (_meta(soup, "og:type") or "").lower()
    strong = bool(nodes) or og_type == "article"
    medium = bool(soup.find("article")) and bool(h1) and bool(published)
    weak = bool(h1) and bool(published) and words >= 250

    return {
        "title": title, "published": published, "description": desc,
        "meta_keywords": meta_keywords, "h1": h1, "word_count": words,
        "is_article": strong or medium, "weak_article": weak,
    }
