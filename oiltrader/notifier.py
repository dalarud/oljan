"""Notifications: ntfy (recommended) / Telegram / console.

ntfy (https://ntfy.sh) is the recommended free channel for near-realtime push:
no account needed — pick a long, unguessable topic, subscribe to it in the ntfy
phone app, and the daemon POSTs to it. Telegram is also supported. Both handle
dedup (don't re-push the same story), quiet hours and optional chart images.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, time, timedelta, timezone
from typing import Optional

import requests

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

log = logging.getLogger("oljan.notifier")


class Notifier:
    def __init__(self, cfg, storage):
        self.cfg = cfg
        self.storage = storage
        self.channel = cfg.get("notifications.channel", "console")
        self.send_charts = cfg.get("notifications.send_charts", True)
        self.dedup_minutes = cfg.get("notifications.dedup_minutes", 30)
        self.quiet_hours = cfg.get("notifications.quiet_hours", []) or []
        tzname = cfg.get("notifications.timezone", "UTC")
        self.tz = ZoneInfo(tzname) if (ZoneInfo and tzname) else timezone.utc

        # Telegram
        self.token = cfg.secret("TELEGRAM_BOT_TOKEN")
        self.chat_id = cfg.secret("TELEGRAM_CHAT_ID")

        # ntfy
        self.ntfy_server = str(cfg.get("notifications.ntfy.server",
                                       "https://ntfy.sh")).rstrip("/")
        self.ntfy_topic = cfg.get("notifications.ntfy.topic", "")
        self.ntfy_priority = str(cfg.get("notifications.ntfy.priority", "high"))
        self.ntfy_token = cfg.secret("NTFY_TOKEN")

        if self.channel == "telegram" and not (self.token and self.chat_id):
            log.warning("channel=telegram but TELEGRAM_BOT_TOKEN/CHAT_ID missing;"
                        " falling back to console.")
            self.channel = "console"
        if self.channel == "ntfy" and (not self.ntfy_topic
                                       or "CHANGE-ME" in self.ntfy_topic):
            log.warning("channel=ntfy but no valid topic set; falling back to "
                        "console. Set notifications.ntfy.topic in config.yaml.")
            self.channel = "console"

    # ------------------------------------------------------------------ public
    def notify_event(self, analysis, chart_path: Optional[str] = None) -> bool:
        dedup = self._dedup_key(analysis)
        if self._is_duplicate(dedup):
            log.info("Suppressing duplicate notification: %s", dedup)
            return False
        if self._in_quiet_hours():
            log.info("In quiet hours; skipping push for %s", dedup)
            return False

        ok = self._send(analysis.message, chart_path)
        if ok:
            eid = analysis.event.event_id
            self.storage.record_notification(eid, "event", dedup)
        return ok

    def heartbeat(self, text: str) -> None:
        self.send_ambient(text)

    def send_text(self, text: str) -> bool:
        """Send unconditionally (used by the morning report and startup ping)."""
        return self._send(text, None)

    def send_ambient(self, text: str) -> bool:
        """Send a non-urgent/periodic message, suppressed during quiet hours."""
        if self._in_quiet_hours():
            log.info("In quiet hours; skipping ambient push.")
            return False
        return self._send(text, None)

    def in_quiet_hours(self) -> bool:
        return self._in_quiet_hours()

    # ---------------------------------------------------------------- internal
    def _send(self, text: str, chart_path: Optional[str]) -> bool:
        if self.channel == "ntfy":
            return self._send_ntfy(text, chart_path)
        if self.channel == "telegram":
            return self._send_telegram(text, chart_path)
        # console fallback
        print("\n" + "=" * 70)
        print(text)
        if chart_path:
            print(f"[chart] {chart_path}")
        print("=" * 70 + "\n")
        return True

    def _send_ntfy(self, text: str, chart_path: Optional[str]) -> bool:
        """Send the full (UTF-8, multi-line) analysis as the POST body, then
        optionally attach the chart as a companion image message.

        Rationale: ntfy attachments require the message to live in HTTP headers
        (single-line, ASCII-only). To keep the rich Swedish text with emoji and
        newlines intact, that must go in the request *body* (POST), so the chart
        is delivered as a second, grouped message with a header-safe caption.
        """
        url = f"{self.ntfy_server}/{self.ntfy_topic}"
        title = _ascii_header(_first_line(text))
        auth = ({"Authorization": f"Bearer {self.ntfy_token}"}
                if self.ntfy_token else {})
        try:
            headers = {"Title": title, "Priority": self.ntfy_priority,
                       "Tags": "oil,fuelpump", "Markdown": "yes", **auth}
            resp = requests.post(url, data=text.encode("utf-8"),
                                 headers=headers, timeout=30)
            if resp.status_code != 200:
                log.error("ntfy send failed (%s): %s", resp.status_code,
                          resp.text[:200])
                return False

            if chart_path and self.send_charts:
                try:
                    with open(chart_path, "rb") as fh:
                        put_headers = {
                            "Title": _ascii_header(_truncate(f"Chart: {title}", 90)),
                            "Priority": self.ntfy_priority,
                            "Filename": "chart.png", "Tags": "chart_with_upwards_trend",
                            **auth,
                        }
                        r2 = requests.put(url, data=fh, headers=put_headers,
                                          timeout=30)
                    if r2.status_code != 200:
                        log.warning("ntfy chart attach failed (%s)",
                                    r2.status_code)
                except Exception as e:
                    log.warning("ntfy chart attach error: %s", e)
            return True
        except Exception as e:
            log.error("ntfy send error: %s", e)
            return False

    def _send_telegram(self, text: str, chart_path: Optional[str]) -> bool:
        base = f"https://api.telegram.org/bot{self.token}"
        # HTML mode: only <, >, & are special (all escaped), so arbitrary news
        # titles with *, _, [, ` never break the parser (Markdown mode did).
        body = _to_telegram_html(text)
        try:
            if chart_path and self.send_charts:
                with open(chart_path, "rb") as fh:
                    resp = requests.post(
                        f"{base}/sendPhoto",
                        data={"chat_id": self.chat_id,
                              "caption": _truncate(body, 1024),
                              "parse_mode": "HTML"},
                        files={"photo": fh}, timeout=30)
                if resp.status_code == 200:
                    return True
                log.warning("sendPhoto failed (%s); sending text only",
                            resp.text[:200])
            resp = requests.post(
                f"{base}/sendMessage",
                data={"chat_id": self.chat_id, "text": _truncate(body, 4096),
                      "parse_mode": "HTML",
                      "disable_web_page_preview": True}, timeout=30)
            if resp.status_code != 200:
                log.error("Telegram sendMessage failed: %s", resp.text[:200])
                # Last-resort: retry as plain text so the alert still lands.
                resp = requests.post(
                    f"{base}/sendMessage",
                    data={"chat_id": self.chat_id,
                          "text": _truncate(_strip_markup(text), 4096),
                          "disable_web_page_preview": True}, timeout=30)
                return resp.status_code == 200
            return True
        except Exception as e:
            log.error("Telegram send error: %s", e)
            return False

    def _dedup_key(self, analysis) -> str:
        ev = analysis.event
        # Same category + direction + rounded title => treat as duplicate.
        title_key = "".join(ch for ch in ev.item.title.lower()
                            if ch.isalnum())[:40]
        return f"{ev.category}:{ev.direction}:{title_key}"

    def _is_duplicate(self, dedup: str) -> bool:
        since = datetime.now(timezone.utc) - timedelta(minutes=self.dedup_minutes)
        return self.storage.notified_since(dedup, since)

    def _in_quiet_hours(self) -> bool:
        if not self.quiet_hours:
            return False
        return _time_in_windows(datetime.now(self.tz).time(), self.quiet_hours)


def _parse_hhmm(s: str) -> time:
    h, m = s.strip().split(":")
    return time(int(h), int(m))


def _time_in_windows(now: time, windows) -> bool:
    """True if `now` falls in any "HH:MM-HH:MM" window (midnight-wrap aware)."""
    for window in windows:
        try:
            start_s, end_s = window.split("-")
            start, end = _parse_hhmm(start_s), _parse_hhmm(end_s)
        except Exception:
            continue
        if start <= end:
            if start <= now <= end:
                return True
        else:  # wraps midnight
            if now >= start or now <= end:
                return True
    return False


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _to_telegram_html(text: str) -> str:
    """Render our lightweight *bold*/_italic_ markup as Telegram HTML.

    HTML parse mode only treats &, <, > as special (all escaped here), so a
    news title containing *, _, [ or ` can never produce an unterminated-entity
    error the way Markdown mode did. Emphasis markers we control are converted
    to <b>/<i>; stray markers survive harmlessly as literal text.
    """
    esc = html.escape(text, quote=False)              # & < >
    esc = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", esc)  # *bold* (balanced, 1 line)
    esc = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"<i>\1</i>", esc)  # _italic_
    return esc


def _strip_markup(text: str) -> str:
    """Plain-text fallback: drop emphasis markers, no parse mode at all."""
    return text.replace("*", "").replace("`", "")


def _first_line(text: str) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    # Prefer the informative headline line (e.g. "[INVENTORY · hausse] ...").
    for line in lines:
        if "[" in line and "]" in line:
            return line.replace("*", "").strip()[:120]
    return (lines[0].replace("*", "").strip()[:120] if lines else "Oljan")


_TRANSLIT = str.maketrans({
    "å": "a", "ä": "a", "ö": "o", "Å": "A", "Ä": "A", "Ö": "O",
    "é": "e", "è": "e", "ü": "u", "·": "-", "–": "-", "—": "-",
})


def _ascii_header(text: str) -> str:
    # ntfy decodes header values as UTF-8 but the HTTP client sends them as
    # latin-1, so keep the Title strictly ASCII (the full UTF-8 text lives in
    # the message body). Transliterate common Swedish chars, drop the rest.
    cleaned = text.replace("*", "").translate(_TRANSLIT)
    cleaned = cleaned.encode("ascii", "ignore").decode("ascii").strip()
    return cleaned or "Oljan"
