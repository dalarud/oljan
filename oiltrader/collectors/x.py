"""X / Twitter collector for intelligence-grade, early sources.

Free X access via the official API is heavily restricted, so this collector
reads a curated list of accounts through Nitter RSS instances (no key needed),
falling back across instances for resilience. If you have an official X API v2
bearer token, set X_BEARER_TOKEN and it will be preferred.

The curated accounts are ones widely recognised for being *early* on
market-moving oil information — headline relays that mirror Bloomberg/Reuters
terminal speed, physical-tanker trackers, and established OSINT/geopolitics
desks. Each is documented in config.example.yaml with its rationale.
"""
from __future__ import annotations

import calendar
import logging
import re
from datetime import datetime, timezone
from typing import Iterable

import requests

from .base import Collector, NewsItem, now_utc

log = logging.getLogger("oljan.collectors.x")


def _rewrite_to_x(url: str, account: str) -> str:
    """Turn a nitter status URL into the canonical x.com URL."""
    m = re.search(r"/status/(\d+)", url or "")
    if m:
        return f"https://x.com/{account}/status/{m.group(1)}"
    return f"https://x.com/{account}"


class XCollector(Collector):
    name = "x"

    def __init__(self, accounts: list[str], nitter_instances: list[str],
                 bearer_token: str = "", per_account: int = 12):
        self.accounts = [a.lstrip("@") for a in (accounts or [])]
        self.instances = [i.rstrip("/") for i in (nitter_instances or [])]
        self.bearer = bearer_token
        self.per_account = per_account
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "Mozilla/5.0 "
                                      "(oljan-oil-monitor)"})
        self._good_instance: str | None = None

    def collect(self) -> Iterable[NewsItem]:
        if not self.accounts:
            return []
        if self.bearer:
            try:
                return self._collect_api()
            except Exception as e:
                log.warning("X API failed (%s); falling back to nitter", e)
        return self._collect_nitter()

    # -------------------------------------------------------------- nitter
    def _collect_nitter(self) -> list[NewsItem]:
        items: list[NewsItem] = []
        for account in self.accounts:
            got = self._fetch_account(account)
            items.extend(got)
        return items

    def _fetch_account(self, account: str) -> list[NewsItem]:
        # Prefer the instance that worked last time.
        order = ([self._good_instance] if self._good_instance else []) + \
                [i for i in self.instances if i != self._good_instance]
        for inst in order:
            if not inst:
                continue
            try:
                url = f"{inst}/{account}/rss"
                resp = self._session.get(url, timeout=15)
                if resp.status_code != 200 or b"<item" not in resp.content:
                    continue
                parsed = self._parse_rss(resp.content, account)
                if parsed:
                    self._good_instance = inst
                    return parsed[: self.per_account]
            except Exception as e:
                log.debug("nitter %s/%s failed: %s", inst, account, e)
        log.debug("no working nitter instance for @%s", account)
        return []

    def _parse_rss(self, content: bytes, account: str) -> list[NewsItem]:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(content)

        def tag(e):
            return e.tag.rsplit("}", 1)[-1]

        out: list[NewsItem] = []
        for it in [e for e in root.iter() if tag(e) == "item"]:
            title, link, desc, ts = "", "", "", now_utc()
            for c in it:
                t = tag(c)
                if t == "title":
                    title = (c.text or "").strip()
                elif t == "link":
                    link = (c.text or c.get("href") or "").strip()
                elif t == "description":
                    desc = c.text or desc
                elif t == "pubDate":
                    ts = _parse_rfc822(c.text) or ts
            clean_title = _strip_html(title)
            if _is_noise(clean_title):
                continue
            out.append(NewsItem(
                source=f"x/@{account}",
                title=clean_title,
                content=_strip_html(desc),
                url=_rewrite_to_x(link, account),
                ts=ts,
                extra={"handle": account, "platform": "x"},
            ))
        return out

    # ---------------------------------------------------------------- API
    def _collect_api(self) -> list[NewsItem]:
        items: list[NewsItem] = []
        headers = {"Authorization": f"Bearer {self.bearer}"}
        for account in self.accounts:
            # Resolve user id then recent tweets.
            u = self._session.get(
                f"https://api.twitter.com/2/users/by/username/{account}",
                headers=headers, timeout=15)
            u.raise_for_status()
            uid = u.json()["data"]["id"]
            r = self._session.get(
                f"https://api.twitter.com/2/users/{uid}/tweets",
                headers=headers, timeout=15,
                params={"max_results": self.per_account,
                        "tweet.fields": "created_at"})
            r.raise_for_status()
            for tw in r.json().get("data", []):
                ts = _parse_iso(tw.get("created_at")) or now_utc()
                items.append(NewsItem(
                    source=f"x/@{account}",
                    title=tw.get("text", "")[:120],
                    content=tw.get("text", ""),
                    url=f"https://x.com/{account}/status/{tw.get('id')}",
                    ts=ts, extra={"handle": account, "platform": "x"}))
        return items


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _is_noise(text: str) -> bool:
    """Filter low-value tweets: retweets, fragments, coordinate/number dumps."""
    t = (text or "").strip()
    if not t:
        return True
    if t.startswith("RT ") or t.startswith("R to @") or t.startswith("R to "):
        return True
    if len(t) < 18:                       # too short to carry a claim
        return True
    non_alpha = sum(1 for c in t if not (c.isalpha() or c.isspace()))
    if non_alpha / len(t) > 0.55:         # coordinates, price/number dumps
        return True
    return False


def _parse_rfc822(text: str | None):
    if not text:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(text)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _parse_iso(text: str | None):
    if not text:
        return None
    try:
        dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ")
    except Exception:
        try:
            dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return None
    return dt.replace(tzinfo=timezone.utc)
