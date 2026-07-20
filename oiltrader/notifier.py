"""Notifications: Telegram (near-realtime, free) with console fallback.

Handles dedup (don't re-push the same story), quiet hours, and optional chart
images. Telegram is the recommended free channel: create a bot with @BotFather,
put the token + your chat id in .env.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Optional

import requests

log = logging.getLogger("oljan.notifier")


class Notifier:
    def __init__(self, cfg, storage):
        self.cfg = cfg
        self.storage = storage
        self.channel = cfg.get("notifications.channel", "console")
        self.token = cfg.secret("TELEGRAM_BOT_TOKEN")
        self.chat_id = cfg.secret("TELEGRAM_CHAT_ID")
        self.send_charts = cfg.get("notifications.send_charts", True)
        self.dedup_minutes = cfg.get("notifications.dedup_minutes", 30)
        self.quiet_hours = cfg.get("notifications.quiet_hours", []) or []

        if self.channel == "telegram" and not (self.token and self.chat_id):
            log.warning("channel=telegram but TELEGRAM_BOT_TOKEN/CHAT_ID missing;"
                        " falling back to console.")
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
        self._send(text, None)

    def send_text(self, text: str) -> bool:
        return self._send(text, None)

    # ---------------------------------------------------------------- internal
    def _send(self, text: str, chart_path: Optional[str]) -> bool:
        if self.channel == "telegram":
            return self._send_telegram(text, chart_path)
        # console fallback
        print("\n" + "=" * 70)
        print(text)
        if chart_path:
            print(f"[chart] {chart_path}")
        print("=" * 70 + "\n")
        return True

    def _send_telegram(self, text: str, chart_path: Optional[str]) -> bool:
        base = f"https://api.telegram.org/bot{self.token}"
        try:
            if chart_path and self.send_charts:
                with open(chart_path, "rb") as fh:
                    resp = requests.post(
                        f"{base}/sendPhoto",
                        data={"chat_id": self.chat_id,
                              "caption": _truncate(text, 1024),
                              "parse_mode": "Markdown"},
                        files={"photo": fh}, timeout=30)
                if resp.status_code == 200:
                    return True
                log.warning("sendPhoto failed (%s); sending text only",
                            resp.text[:200])
            resp = requests.post(
                f"{base}/sendMessage",
                data={"chat_id": self.chat_id, "text": _truncate(text, 4096),
                      "parse_mode": "Markdown",
                      "disable_web_page_preview": True}, timeout=30)
            if resp.status_code != 200:
                log.error("Telegram sendMessage failed: %s", resp.text[:200])
                return False
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
        now = datetime.now(timezone.utc).time()
        for window in self.quiet_hours:
            try:
                start_s, end_s = window.split("-")
                start = _parse_hhmm(start_s)
                end = _parse_hhmm(end_s)
            except Exception:
                continue
            if start <= end:
                if start <= now <= end:
                    return True
            else:  # wraps midnight
                if now >= start or now <= end:
                    return True
        return False


def _parse_hhmm(s: str) -> time:
    h, m = s.strip().split(":")
    return time(int(h), int(m))


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
