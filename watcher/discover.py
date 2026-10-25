"""Find blog/article URLs: RSS/Atom feeds + sitemaps + listing pages (homepage-linked or conventional)."""
import gzip
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .extract import parse_date
from .fetch import FetchError, normalize_url, url_key

BLOG_WORDS = {"blog", "blogs", "resources", "resource", "articles", "article", "news", "insights",
              "learn", "guides", "post", "posts", "stories", "library", "updates", "journal",
              "academy", "knowledge", "press", "newsroom", "perspectives", "editorial", "magazine"}
SKIP_SEG = {"tag", "tags", "category", "categories", "author", "authors", "page", "topics", "topic",
            "feed", "search", "archive", "archives", "rss", "wp-content", "wp-json", "cdn-cgi"}
ASSET_RE = re.compile(r"\.(jpe?g|png|gif|svg|webp|pdf|zip|css|js|xml|ico|mp4|mp3|woff2?)$", re.I)
LISTING_TEXT = {"blog", "resources", "articles", "news", "insights", "learn", "stories", "guides",
                "newsroom", "journal", "library", "resource center", "resource hub"}
DEFAULT_LISTINGS = ["/blog", "/resources", "/articles", "/news", "/insights", "/learn"]
FEED_PATHS = ["/feed", "/feed/", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml",
              "/blog/feed", "/blog/feed/", "/blog/rss", "/blog/rss.xml", "/blog/feed.xml", "/blog/atom.xml"]
SITEMAP_PATHS = ["/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/sitemap-index.xml",
                 "/post-sitemap.xml", "/blog-sitemap.xml"]
PRIORITY_SM = re.compile(r"post|blog|article|news|resource|insight|learn|stor|guide", re.I)


def looks_like_post(url):
    p = urlparse(url)
    if ASSET_RE.search(p.path):
        return False
    segs = [s for s in p.path.split("/") if s]
    if not segs or any(s.lower() in SKIP_SEG for s in segs):
        return False
    if re.fullmatch(r"\d+", segs[-1]) and len(segs) <= 2:
        return False
    host = (p.hostname or "").lower()
    if host.startswith(("blog.", "resources.", "news.", "insights.", "learn.")):
        return True
    if any(s.lower() in BLOG_WORDS for s in segs[:-1]) and segs[-1].lower() not in BLOG_WORDS:
        return True
    if re.search(r"/20\d\d/(0?[1-9]|1[0-2])/", p.path):
        return True
    return False


def _links(soup, base):
    out = []
    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        if not h or h.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        out.append((urljoin(base, h), a.get_text(" ", strip=True), a))
    return out


def _add(cands, fetcher, url, source, title=None, date=None, desc=None):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not fetcher.in_scope(p.hostname):
        return
    url = normalize_url(url)
    k = url_key(url)
    cur = cands.get(k)
    if cur is None:
        cands[k] = {"url": url, "source": source, "title": title, "date": date, "description": desc}
        return
    # prefer richer data (feeds beat sitemaps beat listing links)
    for f, v in (("title", title), ("date", date), ("description", desc)):
        if v and not cur.get(f):
            cur[f] = v
    if source == "feed":
        cur["source"] = "feed"


# ---------------------------------------------------------------- feeds
def _parse_feed(content):
    soup = BeautifulSoup(content, "xml")
    items = []
    for it in soup.find_all(["item", "entry"]):
        link = None
        if it.name == "entry":
            ls = it.find_all("link")
            alt = [l for l in ls if l.get("rel", "alternate") in ("alternate", ["alternate"])]
            tag = (alt or ls or [None])[0]
            link = tag.get("href") if tag else None
        else:
            l = it.find("link")
            link = (l.get_text(strip=True) if l else None) or None
            if not link:
                g = it.find("guid")
                link = g.get_text(strip=True) if g and g.get_text(strip=True).startswith("http") else None
        if not link:
            continue
        t = it.find("title")
        d = it.find(["pubDate", "published", "updated", "date"])
        s = it.find(["description", "summary"])
        desc = None
        if s:
            desc = BeautifulSoup(s.get_text(), "lxml").get_text(" ", strip=True)[:500] or None
        items.append((link.strip(), t.get_text(strip=True) if t else None,
                      parse_date(d.get_text(strip=True)) if d else None, desc))
    return items


def _feed_urls(fetcher, home_soup, base):
    urls = []
    for l in home_soup.find_all("link", rel=lambda r: r and "alternate" in r):
        if any(x in (l.get("type") or "").lower() for x in ("rss", "atom", "xml")) and l.get("href"):
            urls.append(urljoin(base, l["href"]))
    return urls


def _from_feeds(fetcher, home_soup, base, cands, notes):
    declared = _feed_urls(fetcher, home_soup, base)
    tried = list(dict.fromkeys(declared + [urljoin(base, p) for p in FEED_PATHS]))
    got = 0
    for fu in tried[:12]:
        try:
            r = fetcher.get(fu)
        except FetchError:
            continue
        head = r.content[:600].lower()
        if b"<rss" not in head and b"<feed" not in head and b"<?xml" not in head and b"<rdf" not in head:
            continue
        items = _parse_feed(r.content)
        for link, title, date, desc in items:
            _add(cands, fetcher, link, "feed", title, date, desc)
        if items:
            got += len(items)
            notes.append(f"feed: {len(items)} entries from {urlparse(fu).path}")
            if fu in declared and got >= 5:
                break
        if got >= 10 and fu not in declared:
            break


# ---------------------------------------------------------------- sitemaps
def _parse_sitemap(content, url):
    if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
        try:
            content = gzip.decompress(content)
        except OSError:
            return [], []
    soup = BeautifulSoup(content, "xml")

    def pairs(tag):
        out = []
        for el in soup.find_all(tag):
            loc = el.find("loc")
            if loc and loc.get_text(strip=True):
                lm = el.find("lastmod")
                out.append((loc.get_text(strip=True), parse_date(lm.get_text(strip=True)) if lm else None))
        return out
    return pairs("sitemap"), pairs("url")


def _from_sitemaps(fetcher, base, cands, notes):
    queue = list(dict.fromkeys(fetcher.sitemaps + [urljoin(base, p) for p in SITEMAP_PATHS]))
    seen, fetched, found = set(), 0, 0
    while queue and fetched < 25:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            r = fetcher.get(sm)
        except FetchError:
            continue
        fetched += 1
        subs, urls = _parse_sitemap(r.content, sm)
        if subs:
            subs.sort(key=lambda x: (0 if PRIORITY_SM.search(x[0]) else 1))
            queue = [s for s, _ in subs] + queue
        for loc, lm in urls:
            if looks_like_post(loc) and found < 3000:
                _add(cands, fetcher, loc, "sitemap", date=lm)
                found += 1
    if found:
        notes.append(f"sitemap: {found} blog-like URLs in {fetched} file(s)")


# ---------------------------------------------------------------- listing pages
def _next_link(soup, base):
    l = soup.find("link", rel="next")
    if l and l.get("href"):
        return urljoin(base, l["href"])
    a = soup.find("a", rel=lambda r: r and "next" in r, href=True)
    if a:
        return urljoin(base, a["href"])
    for a in soup.find_all("a", href=True):
        t = a.get_text(" ", strip=True).lower()
        if t in ("next", "next page", "older", "older posts", "next \u203a", "\u203a", "\u00bb", "next \u00bb"):
            return urljoin(base, a["href"])
    return None


def _listing_candidates(fetcher, home_links, base):
    out = []
    for url, text, _a in home_links:
        p = urlparse(url)
        if not fetcher.in_scope(p.hostname):
            continue
        segs = [s for s in p.path.split("/") if s]
        host = (p.hostname or "").lower()
        is_sub = host.startswith(("blog.", "resources.", "news.", "insights.", "learn.")) and len(segs) == 0
        by_path = 1 <= len(segs) <= 2 and segs[-1].lower() in BLOG_WORDS
        by_text = text.strip().lower() in LISTING_TEXT and len(segs) <= 2
        if is_sub or by_path or by_text:
            out.append(normalize_url(url))
    out += [normalize_url(urljoin(base, p)) for p in DEFAULT_LISTINGS]
    return list(dict.fromkeys(out))[:8]


def _scan_listing(fetcher, url, cands, max_pages):
    posts_here, page_url, seen = 0, url, set()
    for _ in range(max_pages):
        if page_url in seen:
            break
        seen.add(page_url)
        try:
            r = fetcher.get(page_url)
        except FetchError:
            break
        soup = BeautifulSoup(r.content, "lxml")
        final = getattr(r, "final_url", page_url)
        base_path = urlparse(final).path.rstrip("/")
        before = posts_here
        for link, text, a in _links(soup, final):
            p = urlparse(link)
            if not fetcher.in_scope(p.hostname) or normalize_url(link) == normalize_url(final):
                continue
            if looks_like_post(link):
                _add(cands, fetcher, link, "listing", title=text or None)
                posts_here += 1
        # card layouts: <article> or h2/h3 links (covers blogs whose posts live at /<slug>)
        cards = [art.find("a", href=True) for art in soup.find_all("article")]
        cards += [h.find("a", href=True) for h in soup.find_all(["h2", "h3"])]
        for a in cards:
            if not a:
                continue
            link = urljoin(final, a["href"])
            p = urlparse(link)
            segs = [s for s in p.path.split("/") if s]
            if (fetcher.in_scope(p.hostname) and segs and not ASSET_RE.search(p.path)
                    and not any(s.lower() in SKIP_SEG for s in segs)
                    and normalize_url(link) != normalize_url(final)):
                _add(cands, fetcher, link, "listing", title=a.get_text(" ", strip=True) or None)
                posts_here += 1
        if posts_here == before:
            break
        nxt = _next_link(soup, final)
        if not nxt:
            break
        page_url = normalize_url(nxt)
    return posts_here


def discover(fetcher, home_response, base, max_list_pages=3):
    """Return (candidates, notes). Candidates keyed by url_key."""
    cands, notes = {}, []
    home_soup = BeautifulSoup(home_response.content, "lxml")
    home_final = getattr(home_response, "final_url", base)
    home_links = _links(home_soup, home_final)

    _from_feeds(fetcher, home_soup, base, cands, notes)
    _from_sitemaps(fetcher, base, cands, notes)

    for link, text, _a in home_links:           # posts linked straight from the homepage
        if looks_like_post(link):
            _add(cands, fetcher, link, "homepage", title=text or None)

    listings = _listing_candidates(fetcher, home_links, base)
    listing_hits = []
    for lu in listings:
        n = _scan_listing(fetcher, lu, cands, max_list_pages)
        if n:
            listing_hits.append(f"{urlparse(lu).netloc}{urlparse(lu).path} ({n})")
    if listing_hits:
        notes.append("listing pages: " + ", ".join(listing_hits[:4]))
    return cands, notes
