import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import cfg

_init_lock = threading.Lock()
_initialised_path = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain TEXT UNIQUE NOT NULL,
  base_url TEXT NOT NULL,
  slack_webhook TEXT,
  email TEXT,
  created_at TEXT NOT NULL,
  last_checked_at TEXT,
  last_status TEXT
);
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  url TEXT NOT NULL,
  url_key TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'post',      -- post | ignored | stub
  title TEXT,
  published TEXT,
  description TEXT,
  source TEXT,
  first_seen_at TEXT NOT NULL,
  first_seen_run INTEGER,
  keyword TEXT,
  volume INTEGER,
  cpc REAL,
  competition REAL,
  enrich_status TEXT,
  UNIQUE(domain_id, url_key)
);
CREATE INDEX IF NOT EXISTS idx_posts_domain ON posts(domain_id, kind);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER,
  domain TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,                    -- running | ok | no_blog | blocked | unreachable | error
  progress TEXT,
  result_json TEXT
);
CREATE TABLE IF NOT EXISTS pricing_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  url TEXT NOT NULL,
  taken_at TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  prices_json TEXT NOT NULL,
  headings_json TEXT NOT NULL
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conn():
    global _initialised_path
    path = cfg.DB_PATH
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    with _init_lock:
        if _initialised_path != path:
            c.executescript(SCHEMA)
            _initialised_path = path
    try:
        yield c
        c.commit()
    finally:
        c.close()


def rows(sql, args=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None


def execute(sql, args=()):
    with conn() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def upsert_domain(domain, base_url, slack_webhook=None, email=None):
    with conn() as c:
        row = c.execute("SELECT id FROM domains WHERE domain=?", (domain,)).fetchone()
        if row:
            c.execute(
                "UPDATE domains SET base_url=?, slack_webhook=COALESCE(?, slack_webhook), email=COALESCE(?, email) WHERE id=?",
                (base_url, slack_webhook or None, email or None, row["id"]),
            )
            return row["id"]
        cur = c.execute(
            "INSERT INTO domains(domain, base_url, slack_webhook, email, created_at) VALUES (?,?,?,?,?)",
            (domain, base_url, slack_webhook or None, email or None, now()),
        )
        return cur.lastrowid


def dumps(o):
    return json.dumps(o, ensure_ascii=False)
