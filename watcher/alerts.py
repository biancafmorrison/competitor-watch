"""Slack webhook + SMTP email alerts."""
import re
import smtplib
from email.mime.text import MIMEText

import requests

from .config import cfg
import os

SLACK_RE = re.compile(r"^https://hooks\.slack\.com/(services|workflows)/[A-Za-z0-9/_-]+$")


def valid_slack_webhook(url):
    return bool(SLACK_RE.match(url or ""))


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _kw_line(p):
    if p.get("keyword"):
        vol = p.get("volume")
        v = f"{vol:,}/mo" if isinstance(vol, int) else "volume n/a"
        extra = f", CPC ${p['cpc']:.2f}" if isinstance(p.get("cpc"), (int, float)) else ""
        return f'keyword "{p["keyword"]}" - {v}{extra}'
    return "keyword n/a"


def build_message(domain, posts, pricing, baseline, since, total_new, enrichment):
    """Returns (plain_text, slack_mrkdwn, subject)."""
    n = len(posts)
    if baseline:
        head = f"Now monitoring {domain}: indexed {total_new} posts. Latest {n}:"
    else:
        s = f" since {since}" if since else ""
        head = f"{domain} published {total_new} new post{'s' if total_new != 1 else ''}{s}"
    plain, slack = [head], [f":bell: *{_esc(head)}*" if not baseline else f":eyes: *{_esc(head)}*"]
    for p in posts[:25]:
        d = f" ({p['published']})" if p.get("published") else ""
        plain.append(f"- {p['title'] or p['url']}{d}\n  {p['url']}\n  {_kw_line(p)}")
        slack.append(f"\u2022 <{p['url']}|{_esc(p['title'] or p['url'])}>{d}\n    _{_esc(_kw_line(p))}_")
    if len(posts) > 25:
        plain.append(f"...and {len(posts) - 25} more")
        slack.append(f"...and {len(posts) - 25} more")
    if enrichment and not enrichment.get("ok") and posts:
        note = f"Keyword data unavailable: {enrichment.get('error')}"
        plain.append(note)
        slack.append(f":warning: {_esc(note)}")
    if pricing and pricing.get("status") == "changed":
        bits = []
        if pricing.get("removed_prices"):
            bits.append("removed " + ", ".join(pricing["removed_prices"][:8]))
        if pricing.get("added_prices"):
            bits.append("added " + ", ".join(pricing["added_prices"][:8]))
        line = f"Pricing page changed ({'; '.join(bits)}): {pricing['url']}"
        plain.append(line)
        slack.append(f":moneybag: {_esc(line)}")
    subject = f"[Competitor alert] {head}"[:150]
    return "\n".join(plain), "\n".join(slack), subject


def send_slack(webhook, text):
    try:
        r = requests.post(webhook, json={"text": text}, timeout=10)
    except requests.RequestException as e:
        return {"channel": "slack", "ok": False, "detail": f"Could not reach Slack ({e.__class__.__name__})"}
    if r.status_code == 200:
        return {"channel": "slack", "ok": True, "detail": "Delivered to Slack"}
    return {"channel": "slack", "ok": False, "detail": f"Slack rejected the webhook (HTTP {r.status_code}: {r.text[:80]})"}


def send_email(to, subject, body):
    host = os.getenv("SMTP_HOST", "").strip()
    if not host:
        return {"channel": "email", "ok": False, "detail": "SMTP is not configured on the server"}
    port = int(os.getenv("SMTP_PORT", "587") or 587)
    user, pw = os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", "") or user
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    try:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.starttls()
            if user:
                s.login(user, pw)
            s.sendmail(sender, [to], msg.as_string())
        return {"channel": "email", "ok": True, "detail": f"Emailed {to}"}
    except (smtplib.SMTPException, OSError) as e:
        return {"channel": "email", "ok": False, "detail": f"Email failed ({e.__class__.__name__})"}


def deliver(domain_row, plain, slack_text, subject):
    results = []
    hook = domain_row.get("slack_webhook") or cfg.SLACK_WEBHOOK_URL
    if hook:
        if domain_row.get("slack_webhook") and not valid_slack_webhook(hook):
            results.append({"channel": "slack", "ok": False, "detail": "Webhook URL is not a hooks.slack.com URL"})
        else:
            results.append(send_slack(hook, slack_text))
    to = domain_row.get("email") or os.getenv("ALERT_EMAIL_TO", "").strip()
    if to:
        results.append(send_email(to, subject, plain))
    if not results:
        results.append({"channel": "none", "ok": False,
                        "detail": "No Slack webhook or email configured - alert shown here only"})
    return results
