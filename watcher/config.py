import os

def _b(name, default):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")

def _i(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default

# Read lazily so tests / dotenv can change values before first use.
class _Cfg:
    @property
    def DB_PATH(self): return os.getenv("DB_PATH", "data/watch.db")
    @property
    def USER_AGENT(self): return os.getenv("USER_AGENT", "Mozilla/5.0 (compatible; CompetitorWatch/1.0)")
    @property
    def UA_TOKEN(self): return "CompetitorWatch"
    @property
    def REQUEST_TIMEOUT(self): return _i("REQUEST_TIMEOUT", 15)
    @property
    def RESPECT_ROBOTS(self): return _b("RESPECT_ROBOTS", True)
    @property
    def ALLOW_PRIVATE(self): return _b("ALLOW_PRIVATE", False)
    @property
    def MAX_POSTS_PER_RUN(self): return _i("MAX_POSTS_PER_RUN", 60)
    @property
    def ENRICH_MAX(self): return _i("ENRICH_MAX", 25)
    @property
    def MAX_LIST_PAGES(self): return _i("MAX_LIST_PAGES", 3)
    @property
    def MAX_DOMAINS(self): return _i("MAX_DOMAINS", 25)
    @property
    def FIRST_RUN_ALERT_LIMIT(self): return _i("FIRST_RUN_ALERT_LIMIT", 5)
    @property
    def KEYWORD_PROVIDER(self): return os.getenv("KEYWORD_PROVIDER", "none").strip().lower()
    @property
    def KEYWORD_COUNTRY(self): return os.getenv("KEYWORD_COUNTRY", "us").strip().lower()
    @property
    def SLACK_WEBHOOK_URL(self): return os.getenv("SLACK_WEBHOOK_URL", "").strip()
    @property
    def CHECK_INTERVAL_HOURS(self): return _i("CHECK_INTERVAL_HOURS", 0)

cfg = _Cfg()
