import json
import os
import threading
import time

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

load_dotenv()

from watcher import db, pipeline  # noqa: E402
from watcher.config import cfg  # noqa: E402

app = Flask(__name__, static_folder="static", static_url_path="/static")


def _run_view(r):
    out = {k: r[k] for k in ("id", "domain", "started_at", "finished_at", "status", "progress")}
    out["result"] = json.loads(r["result_json"]) if r.get("result_json") else None
    return out


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/health")
def health():
    prov = cfg.KEYWORD_PROVIDER
    configured = {"keywordseverywhere": bool(os.getenv("KEYWORDS_EVERYWHERE_API_KEY")),
                  "dataforseo": bool(os.getenv("DATAFORSEO_LOGIN") and os.getenv("DATAFORSEO_PASSWORD")),
                  "mock": True}.get(prov, False)
    return jsonify(ok=True, keyword_provider=prov, keyword_api_configured=configured,
                   slack_default=bool(cfg.SLACK_WEBHOOK_URL), email_smtp=bool(os.getenv("SMTP_HOST")),
                   scheduler_hours=cfg.CHECK_INTERVAL_HOURS)


@app.post("/api/check")
def check():
    body = request.get_json(silent=True) or {}
    try:
        run_id, domain = pipeline.start_check(body.get("domain"), (body.get("slack_webhook") or "").strip() or None,
                                              (body.get("email") or "").strip() or None)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(run_id=run_id, domain=domain), 202


@app.get("/api/runs/<int:run_id>")
def run_status(run_id):
    r = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    return (jsonify(_run_view(r)) if r else (jsonify(error="Run not found"), 404))


@app.get("/api/domains")
def domains():
    rows = db.rows("""SELECT d.id, d.domain, d.last_checked_at, d.last_status,
        (SELECT COUNT(*) FROM posts p WHERE p.domain_id=d.id AND p.kind='post') AS posts,
        (d.slack_webhook IS NOT NULL) AS has_slack, d.email
        FROM domains d ORDER BY COALESCE(d.last_checked_at, d.created_at) DESC""")
    return jsonify(rows)


@app.get("/api/domains/<int:did>/posts")
def posts(did):
    rows = db.rows("""SELECT title,url,published,description,keyword,volume,cpc,competition,enrich_status,
        first_seen_at, first_seen_run, source FROM posts WHERE domain_id=? AND kind='post'
        ORDER BY COALESCE(published,'0000') DESC, id DESC""", (did,))
    return jsonify(rows)


@app.get("/api/domains/<int:did>/runs")
def runs(did):
    rows = db.rows("SELECT * FROM runs WHERE domain_id=? ORDER BY id DESC LIMIT 15", (did,))
    return jsonify([_run_view(r) for r in rows])


@app.delete("/api/domains/<int:did>")
def delete_domain(did):
    d = db.one("SELECT domain FROM domains WHERE id=?", (did,))
    if d and d["domain"] in pipeline._running:
        return jsonify(error="A check is running for this domain"), 409
    for t in ("posts", "runs", "pricing_snapshots"):
        db.execute(f"DELETE FROM {t} WHERE domain_id=?", (did,))
    db.execute("DELETE FROM domains WHERE id=?", (did,))
    return jsonify(ok=True)


def _scheduler():
    hours = cfg.CHECK_INTERVAL_HOURS
    while True:
        time.sleep(hours * 3600)
        for d in db.rows("SELECT domain, base_url FROM domains"):
            try:
                pipeline.start_check(d["base_url"], wait=True)
            except Exception as e:  # keep the loop alive
                print("scheduled check failed:", d["domain"], e)


if cfg.CHECK_INTERVAL_HOURS > 0 and not os.getenv("WERKZEUG_RUN_MAIN") == "false":
    threading.Thread(target=_scheduler, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)), debug=False, threaded=True)
