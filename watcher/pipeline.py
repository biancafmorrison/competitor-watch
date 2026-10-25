"""Orchestrates one check: crawl -> detect new -> enrich -> pricing -> alert."""
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from . import alerts, db, keywords as kw, pricing
from .config import cfg
from .discover import _links, discover, looks_like_post
from .extract import parse_article
from .fetch import Fetcher, FetchError, normalize_url, url_key

_running = {}
_lock = threading.Lock()

STATUS_FOR_KIND = {"blocked": "blocked", "robots": "blocked", "dns": "unreachable", "timeout": "unreachable",
                   "connection": "unreachable", "not_found": "unreachable", "offsite": "unreachable",
                   "error": "unreachable"}


def parse_domain_input(raw):
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Enter a competitor domain, e.g. example.com")
    explicit = bool(re.match(r"^https?://", raw, re.I))
    p = urlparse(raw if explicit else "https://" + raw)
    host = (p.hostname or "").lower()
    if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", host or "") or \
            ("." not in host and not cfg.ALLOW_PRIVATE):
        raise ValueError("That doesn't look like a valid domain. Try something like example.com")
    netloc = host + (f":{p.port}" if p.port and cfg.ALLOW_PRIVATE else "")
    schemes = [p.scheme] if explicit else ["https", "http"]
    return netloc, schemes


def start_check(raw, slack_webhook=None, email=None, wait=False):
    netloc, schemes = parse_domain_input(raw)
    domain = netloc.removeprefix("www.")
    if slack_webhook and not alerts.valid_slack_webhook(slack_webhook):
        raise ValueError("Slack webhook must look like https://hooks.slack.com/services/...")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("Email address looks invalid")
    if not db.one("SELECT id FROM domains WHERE domain=?", (domain,)) and \
            db.one("SELECT COUNT(*) c FROM domains")["c"] >= cfg.MAX_DOMAINS:
        raise ValueError(f"Domain limit reached ({cfg.MAX_DOMAINS}). Delete one first.")
    with _lock:
        if domain in _running:
            return _running[domain], domain
        domain_id = db.upsert_domain(domain, f"{schemes[0]}://{netloc}", slack_webhook, email)
        run_id = db.execute("INSERT INTO runs(domain_id,domain,started_at,status,progress) VALUES (?,?,?,?,?)",
                            (domain_id, domain, db.now(), "running", "Starting"))
        _running[domain] = run_id
    t = threading.Thread(target=_execute, args=(run_id, domain_id, domain, netloc, schemes), daemon=True)
    t.start()
    if wait:
        t.join()
    return run_id, domain


def _progress(run_id, text):
    db.execute("UPDATE runs SET progress=? WHERE id=?", (text, run_id))


def _execute(run_id, domain_id, domain, netloc, schemes):
    result = {"domain": domain, "notes": [], "new_posts": [], "pricing": None, "alert": [],
              "enrichment": None, "stats": {}, "error": None}
    status = "error"
    try:
        status = _run(run_id, domain_id, domain, netloc, schemes, result)
    except Exception as e:  # never leave a run stuck in "running"
        result["error"] = f"Unexpected error: {e.__class__.__name__}: {e}"
        status = "error"
    finally:
        db.execute("UPDATE runs SET status=?, finished_at=?, progress=?, result_json=? WHERE id=?",
                   (status, db.now(), "Done", db.dumps(result), run_id))
        db.execute("UPDATE domains SET last_checked_at=?, last_status=? WHERE id=?", (db.now(), status, domain_id))
        with _lock:
            _running.pop(domain, None)


def _slug_title(url):
    segs = [s for s in urlparse(url).path.split("/") if s]
    return (segs[-1].replace("-", " ").replace("_", " ").title()) if segs else url


def _run(run_id, domain_id, domain, netloc, schemes, result):
    fetcher = Fetcher(netloc)
    prev_row = db.one("SELECT last_checked_at FROM domains WHERE id=?", (domain_id,))
    since = (prev_row or {}).get("last_checked_at")

    # ---- 1. homepage ------------------------------------------------
    _progress(run_id, "Fetching homepage")
    home, err, base = None, None, None
    for scheme in schemes:
        try:
            home = fetcher.get(f"{scheme}://{netloc}/", allow_offsite=True)
            base = f"{scheme}://{urlparse(home.final_url).netloc}"
            fetcher.root = urlparse(home.final_url).netloc.removeprefix("www.")
            break
        except FetchError as e:
            err = err or e
    if home is None:
        result["error"] = err.message
        result["hint"] = {"blocked": "The site is actively blocking automated requests. Try again later, or set a different "
                                     "USER_AGENT; for sites behind Cloudflare a headless browser is needed.",
                          "robots": "The site's robots.txt disallows crawling. Set RESPECT_ROBOTS=false only if you have permission."
                          }.get(err.kind, "Check the domain and try again.")
        return STATUS_FOR_KIND.get(err.kind, "unreachable")

    # ---- 2. discover ------------------------------------------------
    _progress(run_id, "Looking for blog posts (feeds, sitemaps, listing pages)")
    cands, notes = discover(fetcher, home, base, cfg.MAX_LIST_PAGES)
    result["notes"] += notes
    known = {r["url_key"] for r in db.rows("SELECT url_key FROM posts WHERE domain_id=?", (domain_id,))}
    first_run = len(known) == 0
    unseen = [c for k, c in cands.items() if k not in known]
    unseen.sort(key=lambda c: c.get("date") or "0000", reverse=True)
    to_fetch, overflow = unseen[:cfg.MAX_POSTS_PER_RUN], unseen[cfg.MAX_POSTS_PER_RUN:]

    # ---- 3. fetch + classify unseen candidates ----------------------
    _progress(run_id, f"Reading {len(to_fetch)} new page(s)")
    errors, blocked, ok_pages, seen_keys = [], 0, 0, set(known)

    def work(c):
        try:
            r = fetcher.get(c["url"])
        except FetchError as e:
            return c, None, None, e
        return c, parse_article(r.content, getattr(r, "final_url", c["url"])), getattr(r, "final_url", c["url"]), None

    with ThreadPoolExecutor(max_workers=6) as ex:
        fetched = list(ex.map(work, to_fetch))

    with db.conn() as c:
        for cand, info, final, e in fetched:
            k = url_key(cand["url"])
            if e:
                if e.kind == "not_found":
                    c.execute("INSERT OR IGNORE INTO posts(domain_id,url,url_key,kind,first_seen_at,first_seen_run) "
                              "VALUES (?,?,?,'ignored',?,?)", (domain_id, cand["url"], k, db.now(), run_id))
                else:                      # transient: leave unseen so it is retried next run
                    errors.append(e.message)
                    blocked += e.kind in ("blocked", "robots")
                continue
            ok_pages += 1
            fk = url_key(final)
            accept = info["is_article"] or cand["source"] == "feed" or \
                (info["weak_article"] and looks_like_post(final))
            if accept and fk != k and fk in seen_keys:
                accept = False                       # redirect to a post we already have
            kind = "post" if accept and (info["title"] or cand.get("title")) else "ignored"
            if kind == "post":
                seen_keys.add(k)
                seen_keys.add(fk)
            c.execute(
                "INSERT OR IGNORE INTO posts(domain_id,url,url_key,kind,title,published,description,source,"
                "first_seen_at,first_seen_run,enrich_status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (domain_id, cand["url"], k, kind,
                 (info["title"] or cand.get("title")) if kind == "post" else None,
                 (info["published"] or cand.get("date")) if kind == "post" else None,
                 (info["description"] or cand.get("description")) if kind == "post" else None,
                 cand["source"], db.now(), run_id, "pending" if kind == "post" else None))
            if kind == "post":
                result.setdefault("_meta_kw", {})[k] = info["meta_keywords"]
        for cand in overflow:                         # beyond per-run cap: remember, never alert
            c.execute("INSERT OR IGNORE INTO posts(domain_id,url,url_key,kind,title,published,source,first_seen_at,"
                      "first_seen_run) VALUES (?,?,?,'stub',?,?,?,?,?)",
                      (domain_id, cand["url"], url_key(cand["url"]), cand.get("title") or _slug_title(cand["url"]),
                       cand.get("date"), cand["source"], db.now(), run_id))
    meta_kw = result.pop("_meta_kw", {})
    if overflow:
        result["notes"].append(f"{len(overflow)} older URLs were indexed without fetching (cap MAX_POSTS_PER_RUN={cfg.MAX_POSTS_PER_RUN}).")
    if errors:
        result["notes"].append(f"{len(errors)} page(s) could not be fetched ({errors[0]}); they will be retried next run.")

    new_rows = db.rows("SELECT * FROM posts WHERE domain_id=? AND first_seen_run=? AND kind='post' "
                       "ORDER BY COALESCE(published,'0000') DESC, id DESC", (domain_id, run_id))

    # ---- 4. enrich --------------------------------------------------
    if new_rows:
        _progress(run_id, f"Looking up keyword data for {min(len(new_rows), cfg.ENRICH_MAX)} post(s)")
    _enrich(new_rows, meta_kw, result)
    new_rows = db.rows("SELECT * FROM posts WHERE domain_id=? AND first_seen_run=? AND kind='post' "
                       "ORDER BY COALESCE(published,'0000') DESC, id DESC", (domain_id, run_id))
    result["new_posts"] = [{k: p[k] for k in ("title", "url", "published", "description", "keyword", "volume",
                                              "cpc", "competition", "enrich_status", "source")} for p in new_rows]

    # ---- 5. pricing -------------------------------------------------
    _progress(run_id, "Checking pricing page")
    try:
        result["pricing"] = pricing.check(fetcher, base, _links(BeautifulSoup(home.content, "lxml"), base), domain_id)
    except Exception as e:  # pricing must never break the blog flow
        result["pricing"] = {"status": "error", "note": f"Pricing check failed: {e.__class__.__name__}"}

    # ---- 6. alert ---------------------------------------------------
    post_count = db.one("SELECT COUNT(*) c FROM posts WHERE domain_id=? AND kind='post'", (domain_id,))["c"]
    price_changed = (result["pricing"] or {}).get("status") == "changed"
    alert_posts = result["new_posts"][:cfg.FIRST_RUN_ALERT_LIMIT] if first_run else result["new_posts"]
    if post_count and (new_rows or price_changed):
        plain, slack_text, subject = alerts.build_message(
            domain, alert_posts, result["pricing"], first_run, (since or "")[:10], len(new_rows), result["enrichment"])
        result["alert_text"] = plain
        try:
            result["alert"] = alerts.deliver(db.one("SELECT * FROM domains WHERE id=?", (domain_id,)), plain, slack_text, subject)
        except Exception as e:  # a broken alert channel must not lose the run's results
            result["alert"] = [{"channel": "unknown", "ok": False, "detail": f"Alert delivery failed ({e.__class__.__name__})"}]

    result["stats"] = {"candidates": len(cands), "new": len(new_rows), "fetched": ok_pages,
                       "total_tracked": post_count, "first_run": first_run, "requests": fetcher.request_count}

    # ---- 7. status --------------------------------------------------
    if post_count == 0:
        if blocked and not ok_pages:
            result["error"] = "Pages were found but every request was blocked by the site."
            return "blocked"
        result["error"] = "No blog or article pages were found on this site."
        result["hint"] = ("Checked RSS/Atom feeds, sitemaps, /blog, /resources, /articles, /news and homepage links. "
                          "If the blog is rendered by JavaScript or lives on another domain, enter that domain instead.")
        return "no_blog"
    return "ok"


def _enrich(new_rows, meta_kw, result):
    """Pick a target keyword for each new post, then look up volume for the most recent ENRICH_MAX."""
    if not new_rows:
        return
    chosen = {}
    with db.conn() as c:
        for p in new_rows:
            k = kw.pick_keyword(p["title"], meta_kw.get(p["url_key"], []), p["url"])
            chosen[p["id"]] = k
            c.execute("UPDATE posts SET keyword=? WHERE id=?", (k or None, p["id"]))
    targets = [p for p in new_rows if chosen.get(p["id"])][:cfg.ENRICH_MAX]
    if not targets:
        return
    try:
        data = kw.lookup([chosen[p["id"]] for p in targets])
    except kw.KeywordError as e:
        status = "not_configured" if e.kind == "not_configured" else f"error:{e.kind}"
        with db.conn() as c:
            for p in targets:
                c.execute("UPDATE posts SET enrich_status=? WHERE id=?", (status, p["id"]))
        result["enrichment"] = {"ok": False, "kind": e.kind, "error": e.message}
        return
    hits = 0
    with db.conn() as c:
        for p in targets:
            row = data.get(chosen[p["id"]].lower())
            if row and row.get("volume") is not None:
                hits += 1
                c.execute("UPDATE posts SET volume=?, cpc=?, competition=?, enrich_status='ok' WHERE id=?",
                          (row["volume"], row.get("cpc"), row.get("competition"), p["id"]))
            else:
                c.execute("UPDATE posts SET enrich_status='no_data' WHERE id=?", (p["id"],))
    result["enrichment"] = {"ok": True, "provider": kw.provider_name(), "looked_up": len(targets), "with_volume": hits}
    if len(new_rows) > len(targets):
        result["notes"].append(f"Keyword lookup limited to {len(targets)} most recent posts (ENRICH_MAX).")
