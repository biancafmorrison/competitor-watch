"""HTTP layer: polite, SSRF-safe, redirect-aware, and able to recognise bot blocking."""
import ipaddress
import re
import socket
import threading
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import cfg

TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
            "fbclid", "gclid", "mc_cid", "mc_eid", "ref"}

CHALLENGE_MARKERS = (
    "just a moment...", "cf-chl", "attention required! | cloudflare",
    "enable javascript and cookies to continue", "px-captcha", "datadome",
    "are you a robot", "request blocked", "access denied", "pardon our interruption",
    "verify you are human", "ddos protection by",
)


class FetchError(Exception):
    """kind: blocked | robots | not_found | timeout | dns | connection | offsite | error"""

    def __init__(self, kind, message, status=None):
        super().__init__(message)
        self.kind, self.message, self.status = kind, message, status


def normalize_url(url):
    p = urlparse(url.strip())
    host = (p.hostname or "").lower()
    if p.port and p.port not in (80, 443):
        host += f":{p.port}"
    path = re.sub(r"/{2,}", "/", p.path) or "/"
    if path != "/":
        path = path.rstrip("/")
    q = urlencode([(k, v) for k, v in parse_qsl(p.query) if k.lower() not in TRACKING])
    return urlunparse(((p.scheme or "https").lower(), host, path, "", q, ""))


def url_key(url):
    """Identity of a page: ignores scheme, www., trailing slash, fragments, tracking params."""
    p = urlparse(normalize_url(url))
    host = p.netloc.removeprefix("www.")
    return host + p.path + (("?" + p.query) if p.query else "")


def is_public_host(host):
    if cfg.ALLOW_PRIVATE:
        return True
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True  # let the real request fail with a proper DNS error
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            return False
    return True


class Fetcher:
    def __init__(self, root_host):
        self.root = root_host.lower().removeprefix("www.")
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": cfg.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        retry = Retry(total=2, backoff_factor=0.6, status_forcelist=[500, 502, 504],
                      allowed_methods=["GET"], raise_on_status=False)
        ad = HTTPAdapter(max_retries=retry, pool_maxsize=16)
        self.s.mount("http://", ad)
        self.s.mount("https://", ad)
        self._robots = {}
        self._rlock = threading.Lock()
        self._tlock = threading.Lock()
        self._last = 0.0
        self.sitemaps = []
        self.request_count = 0

    # ---- scope -------------------------------------------------------
    def in_scope(self, host):
        host = (host or "").lower().split(":")[0].removeprefix("www.")
        root = self.root.split(":")[0]
        return host == root or host.endswith("." + root)

    # ---- robots.txt --------------------------------------------------
    def _robots_for(self, scheme, netloc):
        key = netloc.lower()
        with self._rlock:
            if key in self._robots:
                return self._robots[key]
        rp = None
        try:
            r = self.s.get(f"{scheme}://{netloc}/robots.txt", timeout=8)
            if r.status_code == 200 and r.content:
                rp = RobotFileParser()
                rp.parse(r.text.splitlines())
                for sm in (rp.site_maps() or []):
                    if sm not in self.sitemaps:
                        self.sitemaps.append(sm)
        except requests.RequestException:
            rp = None
        with self._rlock:
            self._robots[key] = rp
        return rp

    def can_fetch(self, url):
        if not cfg.RESPECT_ROBOTS:
            return True
        p = urlparse(url)
        rp = self._robots_for(p.scheme, p.netloc)
        return True if rp is None else rp.can_fetch(cfg.UA_TOKEN, url)

    def _throttle(self):
        with self._tlock:
            wait = 0.25 - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    # ---- main entry --------------------------------------------------
    def get(self, url, check_robots=True, allow_offsite=False):
        for _hop in range(6):
            p = urlparse(url)
            if p.scheme not in ("http", "https"):
                raise FetchError("error", f"Unsupported URL scheme: {p.scheme}")
            if not is_public_host(p.hostname):
                raise FetchError("error", "Refusing to fetch a private/internal address")
            if not allow_offsite and not self.in_scope(p.hostname):
                raise FetchError("offsite", f"Redirected outside the target site ({p.hostname})")
            if check_robots and not self.can_fetch(url):
                raise FetchError("robots", f"robots.txt disallows {p.path or '/'}")
            self._throttle()
            self.request_count += 1
            try:
                r = self.s.get(url, timeout=cfg.REQUEST_TIMEOUT, allow_redirects=False)
            except requests.exceptions.Timeout:
                raise FetchError("timeout", "The site took too long to respond")
            except requests.exceptions.SSLError:
                raise FetchError("connection", "TLS/SSL error while connecting")
            except requests.exceptions.ConnectionError as e:
                txt = str(e).lower()
                if "name or service not known" in txt or "nodename nor servname" in txt \
                        or "getaddrinfo" in txt or "name resolution" in txt:
                    raise FetchError("dns", "Domain does not resolve (check the spelling)")
                raise FetchError("connection", "Could not connect to the site")
            except requests.RequestException as e:
                raise FetchError("error", f"Request failed: {e.__class__.__name__}")
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                url = urljoin(url, r.headers["Location"])
                continue
            r.final_url = url
            self._classify(r)
            return r
        raise FetchError("error", "Too many redirects")

    def _classify(self, r):
        status = r.status_code
        ctype = r.headers.get("Content-Type", "").lower()
        head = ""
        if "html" in ctype or "text" in ctype or not ctype:
            head = r.content[:6000].decode("utf-8", "ignore").lower()
        challenge = any(m in head for m in CHALLENGE_MARKERS) or \
            ("captcha" in head and len(r.content) < 20000)
        server = r.headers.get("Server", "").lower()
        if status in (401, 403, 429, 451) or (status in (502, 503) and challenge) \
                or (status == 200 and challenge and len(r.content) < 30000):
            who = "Cloudflare" if "cloudflare" in server or "cf-chl" in head else \
                "bot protection" if challenge else "the server"
            raise FetchError("blocked", f"Request was blocked by {who} (HTTP {status})", status)
        if status in (404, 410):
            raise FetchError("not_found", f"HTTP {status} not found", status)
        if status >= 400:
            raise FetchError("error", f"HTTP {status}", status)
