"""
End-to-end test with no internet needed. Spins up:
  - a fake competitor site (blog, sitemap, feed, pricing page)
  - a fake site that blocks bots (Cloudflare-style challenge)
  - a fake site with no blog
  - a fake Slack webhook receiver
Run:  python tests/test_local.py
"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update(ALLOW_PRIVATE="1", DB_PATH=os.path.join(tempfile.mkdtemp(), "t.db"),
                  KEYWORD_PROVIDER="mock", RESPECT_ROBOTS="true")

from watcher import db, keywords, pipeline  # noqa: E402

STATE = {"posts": [("how-to-do-keyword-research", "How to Do Keyword Research | Acme", "2026-10-01"),
                   ("email-marketing-for-saas", "Email Marketing for SaaS: The Ultimate Guide | Acme", "2026-10-10"),
                   ("best-crm-tools", "10 Best CRM Tools in 2026 | Acme", "2026-10-20")],
         "price": "$49", "slack": []}


def page(title, body="", head=""):
    return f"<html><head><title>{title}</title>{head}</head><body>{body}</body></html>".encode()


class Site(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, code, body, ctype="text/html"):
        self.send_response(code); self.send_header("Content-Type", ctype); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        mode = self.server.mode
        ua = self.headers.get("User-Agent", "")
        p = self.path.split("?")[0]
        if mode == "nobot" and "CompetitorWatch" in ua:
            return self.send(403, b"<html><title>Just a moment...</title>cf-chl challenge</html>")
        if p == "/robots.txt":
            return self.send(200, b"User-agent: *\nDisallow: /admin\nSitemap: http://%s/sitemap.xml\n" % self.headers["Host"].encode(), "text/plain")
        if mode == "noblog":
            if p == "/":
                return self.send(200, page("Acme - Home", "<h1>Acme</h1><a href='/about'>About</a>"))
            return self.send(404, b"nope")
        if p == "/":
            links = "".join(f"<a href='/blog/{s}'>{t}</a>" for s, t, _ in STATE["posts"][-2:])
            return self.send(200, page("Acme", f"<nav><a href='/blog'>Blog</a><a href='/pricing'>Pricing</a></nav>{links}",
                                       "<link rel='alternate' type='application/rss+xml' href='/feed.xml'>"))
        if p == "/blog":
            cards = "".join(f"<article><h2><a href='/blog/{s}'>{t}</a></h2></article>" for s, t, _ in STATE["posts"])
            return self.send(200, page("Blog", cards))
        if p.startswith("/blog/"):
            slug = p.split("/")[-1]
            for s, t, d in STATE["posts"]:
                if s == slug:
                    ld = json.dumps({"@type": "BlogPosting", "headline": t.split(" | ")[0], "datePublished": d})
                    head = (f"<meta name='description' content='Description of {s}'>"
                            f"<meta property='og:type' content='article'>"
                            f"<script type='application/ld+json'>{ld}</script>")
                    return self.send(200, page(t, f"<article><h1>{t.split(' | ')[0]}</h1><p>text</p></article>", head))
            return self.send(404, b"nope")
        if p == "/sitemap.xml":
            host = self.headers["Host"]
            urls = "".join(f"<url><loc>http://{host}/blog/{s}</loc><lastmod>{d}</lastmod></url>" for s, _, d in STATE["posts"])
            return self.send(200, f"<?xml version='1.0'?><urlset>{urls}</urlset>".encode(), "application/xml")
        if p == "/feed.xml":
            host = self.headers["Host"]
            items = "".join(f"<item><title>{t}</title><link>http://{host}/blog/{s}</link><pubDate>{d}</pubDate></item>" for s, t, d in STATE["posts"])
            return self.send(200, f"<?xml version='1.0'?><rss><channel>{items}</channel></rss>".encode(), "application/xml")
        if p == "/pricing":
            return self.send(200, page("Pricing", f"<h1>Pricing</h1><h2>Starter</h2><p>{STATE['price']}/month</p><h2>Pro</h2><p>$99/month</p>"))
        self.send(404, b"nope")


class Hook(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        STATE["slack"].append(json.loads(self.rfile.read(n)))
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")


def serve(handler, mode="normal"):
    s = ThreadingHTTPServer(("127.0.0.1", 0), handler); s.mode = mode
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, f"http://127.0.0.1:{s.server_address[1]}"


def check(url, **kw):
    run_id, _ = pipeline.start_check(url, wait=True, **kw)
    r = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    return r["status"], json.loads(r["result_json"])


def ok(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        ok.failed += 1
ok.failed = 0


hook_srv, hook_url = serve(Hook)
os.environ["SLACK_WEBHOOK_URL"] = hook_url + "/hook"
_, site = serve(Site, "normal")
_, blocked = serve(Site, "nobot")
_, noblog = serve(Site, "noblog")

print("\n== Run 1: first check of a competitor ==")
st, r = check(site)
ok(st == "ok", f"status ok (got {st})")
ok(r["stats"]["new"] == 3, f"found all 3 posts (got {r['stats']['new']})")
p = {x["url"].split("/")[-1]: x for x in r["new_posts"]}
ok(all(x["published"] and x["description"] and x["title"] for x in r["new_posts"]), "title, publish date, meta description extracted")
ok(p["how-to-do-keyword-research"]["keyword"] == "how to do keyword research", "target keyword derived")
ok(all(isinstance(x["volume"], int) for x in r["new_posts"]), "search volume attached")
ok(r["pricing"]["status"] == "baseline", "pricing baseline saved")
ok(len(STATE["slack"]) == 1 and "Acme" in STATE["slack"][0]["text"] or len(STATE["slack"]) == 1, "Slack message sent on first run")

print("\n== Run 2: same domain again (must NOT repeat) ==")
n_before = len(STATE["slack"])
st, r = check(site)
ok(st == "ok" and r["stats"]["new"] == 0, f"0 new posts flagged (got {r['stats']['new']})")
ok(len(STATE["slack"]) == n_before, "no duplicate Slack alert")
ok(r["pricing"]["status"] == "unchanged", "pricing unchanged")

print("\n== Run 3: new post + price change ==")
STATE["posts"].append(("seo-audit-checklist", "SEO Audit Checklist | Acme", "2026-10-23"))
STATE["price"] = "$59"
st, r = check(site)
ok(r["stats"]["new"] == 1 and r["new_posts"][0]["url"].endswith("seo-audit-checklist"), "only the 1 new post flagged")
ok(r["pricing"]["status"] == "changed" and "$59" in " ".join(r["pricing"]["added_prices"]).replace(" ", ""), f"pricing change detected: {r['pricing'].get('removed_prices')} -> {r['pricing'].get('added_prices')}")
txt = STATE["slack"][-1]["text"]
ok("1 new post" in txt and "seo-audit-checklist" in txt and "Pricing page changed" in txt, "Slack alert has post + keyword + pricing change")
print("\n--- sample Slack payload ---\n" + txt + "\n----------------------------")

print("\n== Edge cases ==")
st, r = check(blocked)
ok(st == "blocked" and "blocked" in r["error"].lower(), f"bot-blocking site -> status blocked: {r['error']}")
st, r = check(noblog)
ok(st == "no_blog", f"site with no blog -> status no_blog: {r['error']}")
st, r = check("http://127.0.0.1:1")
ok(st == "unreachable", f"unreachable site handled: {r['error']}")

class Bad:  # fake 401 from the keyword API
    status_code = 401
    def json(self): return {}
real_post = keywords.requests.post
def fake_post(url, *a, **k):
    return Bad() if "keywordseverywhere" in url else real_post(url, *a, **k)
keywords.requests.post = fake_post
os.environ.update(KEYWORD_PROVIDER="keywordseverywhere", KEYWORDS_EVERYWHERE_API_KEY="bad-key")
STATE["posts"].append(("link-building-basics", "Link Building Basics | Acme", "2026-10-24"))
st, r = check(site)
ok(st == "ok" and r["stats"]["new"] == 1, "post still detected with an invalid API key")
ok(r["enrichment"]["kind"] == "auth", f"invalid key reported cleanly: {r['enrichment']['error']}")
ok("Keyword data unavailable" in STATE["slack"][-1]["text"], "alert says keyword data unavailable instead of crashing")
keywords.requests.post = real_post

try:
    pipeline.start_check("not a domain")
    ok(False, "bad input rejected")
except ValueError as e:
    ok(True, f"bad input rejected: {e}")

print("\nFAILED: %d" % ok.failed if ok.failed else "\nALL TESTS PASSED")
sys.exit(1 if ok.failed else 0)
