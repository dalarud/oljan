"""Morning report.

At the end of the quiet window the trader wakes to a single briefing instead of
a night of pings: what happened overnight, where price and the key levels sit
now, and a concrete, time-marked plan for the session — which moves to watch,
when, and against which levels. It is decision support, not a signal service.

Catalyst clock times are derived with zoneinfo from each release's real
scheduling timezone (US Eastern / London), so they stay correct across DST in
both zones and are printed in the trader's local time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

log = logging.getLogger("oljan.morning")


def _weight(e: dict) -> float:
    return float(e.get("relevance") or 0.0) * max(float(e.get("substance") or 0.0),
                                                  0.15)


def _fmt_ts(ts_epoch: int, tz) -> str:
    dt = datetime.fromtimestamp(ts_epoch, tz=timezone.utc)
    if tz is not None:
        dt = dt.astimezone(tz)
    return dt.strftime("%H:%M")


# Recurring day catalysts, defined in their real scheduling timezone so DST is
# handled correctly. (weekday: Mon=0..Sun=6; None = every weekday.)
_CATALYSTS = [
    (None, "Europe/London", (8, 0), "Europaöppning – första riktiga likviditeten"),
    (None, "America/New_York", (9, 30), "US-öppning (aktier/energi) – största volymen"),
    (1, "America/New_York", (16, 30), "API råoljelager (prel., risk för rörelse)"),
    (2, "America/New_York", (10, 30), "EIA veckolagerstatistik – veckans största katalysator"),
    (4, "America/New_York", (13, 0), "Baker Hughes riggräkning"),
]


def _catalysts_today(now_local: datetime, tz) -> list[tuple[str, str]]:
    """Return [(HH:MM local, label)] for today's catalysts, sorted by time."""
    if tz is None or ZoneInfo is None:
        return []
    out = []
    wd = now_local.weekday()
    for want_wd, sched_tz, (hh, mm), label in _CATALYSTS:
        if want_wd is not None and want_wd != wd:
            continue
        if want_wd is None and wd >= 5:  # skip weekend session markers
            continue
        try:
            src = datetime(now_local.year, now_local.month, now_local.day,
                           hh, mm, tzinfo=ZoneInfo(sched_tz))
            local = src.astimezone(tz)
            if local.date() == now_local.date():
                out.append((local.strftime("%H:%M"), label))
        except Exception:
            continue
    out.sort(key=lambda x: x[0])
    return out


def _bias_from_events(events: list[dict]) -> tuple[str, float, int, int]:
    bull = [e for e in events if e.get("direction") == "bullish"]
    bear = [e for e in events if e.get("direction") == "bearish"]
    net = sum(_weight(e) for e in bull) - sum(_weight(e) for e in bear)
    total = sum(_weight(e) for e in events) or 1.0
    nn = net / total
    if nn > 0.4:
        label = "STARK HAUSSE 🟢🟢"
    elif nn > 0.1:
        label = "svag hausse 🟢"
    elif nn < -0.4:
        label = "STARK BAISSE 🔴🔴"
    elif nn < -0.1:
        label = "svag baisse 🔴"
    else:
        label = "neutral/blandat 🟡"
    return label, nn, len(bull), len(bear)


def build_morning_report(cfg, storage, *, name, symbol, chart, levels,
                         mtf_trends, cross, night_hours, tz) -> str:
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz) if tz else now_utc
    since = now_utc - timedelta(hours=night_hours)
    events = [e for e in storage.recent_events(since)
              if e.get("source") not in ("seed", None)]

    from .playbook import compact_plan
    profile = cfg.get("trader_profile", None)
    px = f"{chart.price:.2f}" if chart is not None else "n/a"
    lines: list[str] = [f"🌅 *Morgonrapport {now_local:%H:%M}* · {name} {px}"]

    # 1) One-line situation + top headline.
    if events:
        bias, nn, nb, nbe = _bias_from_events(events)
        lines.append(f"Bias *{bias}* · {len(events)} natthändelser "
                     f"(🟢{nb}/🔴{nbe})")
        top = sorted(events, key=_weight, reverse=True)[0]
        d = {"bullish": "🟢", "bearish": "🔴"}.get(top.get("direction"), "⚪")
        lines.append(f"📰 {d} {(top.get('title') or '').strip()[:90]}")
        if top.get("url"):
            lines.append(f"   🔗 {top['url']}")
    else:
        lines.append("Bias neutral · lugn natt, inga relevanta nyheter.")

    # 2) Levels on one line + a single staleness note.
    if chart is None:
        lines.append("📊 ⚠️ Ingen färsk prisdata – nivåer sätts vid Europaöppning.")
    else:
        res = levels.resistances_above() if levels else []
        sup = levels.supports_below() if levels else []
        piv = (levels.vwap or levels.pdc) if levels else None
        r = "/".join(f"{v:.2f}" for _, v in res[:3]) or "–"
        s = "/".join(f"{v:.2f}" for _, v in sup[:3]) or "–"
        pv = f" · pivot {piv:.2f}" if piv else ""
        lines.append(f"📊 {px} · R {r} · S {s}{pv} · RSI {chart.rsi:.0f}")
        age = getattr(chart, "last_candle_age_min", 0) or 0
        if age > 30:
            lines.append("   _(nivåer = gårdagssession, bekräfta vid 09:00)_")

    # 3) Compact, intelligence-driven plan (regime → levels → your style).
    lines.append("\n🎯 *Plan idag:*")
    lines.extend(compact_plan(events, chart, levels, cross, profile))

    # 4) Catalyst clock (one line).
    cats = _catalysts_today(now_local, tz)
    if cats:
        lines.append("\n🗓 " + " · ".join(f"{t} {l.split(' –')[0].split(' (')[0]}"
                                          for t, l in cats))

    lines.append("_Beslutsstöd, ej rådgivning._")
    return "\n".join(lines)


def _day_plan(chart, levels, events, mtf_trends) -> list[str]:
    if chart is None:
        return ["Avvakta tills prisdata är färsk (oftast vid Europaöppning). "
                "Planera nivåer då; agera inte på tunn nattlikviditet."]
    out: list[str] = []
    price = chart.price
    res = levels.resistances_above() if levels else []
    sup = levels.supports_below() if levels else []
    r1 = res[0][1] if res else (chart.nearest_resistance or None)
    r2 = res[1][1] if len(res) > 1 else None
    s1 = sup[0][1] if sup else (chart.nearest_support or None)
    s2 = sup[1][1] if len(sup) > 1 else None
    pivot = (levels.vwap if levels and levels.vwap else
             (levels.pdc if levels and levels.pdc else price))

    # Bias: blend MTF trend with overnight news net-bias.
    up_tfs = sum(1 for t in (mtf_trends or {}).values() if t == "up")
    dn_tfs = sum(1 for t in (mtf_trends or {}).values() if t == "down")
    _, nn, _, _ = _bias_from_events(events) if events else ("", 0.0, 0, 0)
    score = (up_tfs - dn_tfs) + (1 if nn > 0.1 else -1 if nn < -0.1 else 0)
    if score > 0:
        bias = "long-luta"
    elif score < 0:
        bias = "short-luta"
    else:
        bias = "neutral – låt nivåerna bestämma"

    out.append(f"Pivot (skiljelinje): *{pivot:.2f}* · dagsbias: *{bias}*.")
    out.append("Före 09:00: tunn likviditet, falska utbrott vanliga – vänta på "
               "retest snarare än att jaga.")
    if r1:
        tgt = f" mot {r2:.2f}" if r2 else ""
        out.append(f"↑ Scenario upp: håller priset över *{r1:.2f}* med volym "
                   f"(helst efter Europa-/US-öppning) → sikta{tgt}. "
                   f"Ogiltigt om det faller tillbaka under {pivot:.2f}.")
    if s1:
        tgt = f" mot {s2:.2f}" if s2 else ""
        out.append(f"↓ Scenario ned: tappar priset *{s1:.2f}* → risk{tgt}, "
                   f"särskilt kring US-öppningen då volymen är störst. "
                   f"Ogiltigt tillbaka över {pivot:.2f}.")
    out.append(f"Runt {pivot:.2f}: neutral zon – mindre storlek, vänta på att "
               f"en sida ger vika.")
    if getattr(chart, "atr", 0):
        out.append(f"Dagsrytm: ATR ≈ {chart.atr:.2f} – kalibrera stop/mål efter "
                   f"det, inte tightare än brus.")
    return out
