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

    lines: list[str] = []
    px = f"{chart.price:.2f}" if chart is not None else "n/a"
    ctf = (chart.timeframe or "") if chart is not None else ""
    lines.append(f"🌅 *Oljan – Morgonrapport {now_local:%Y-%m-%d %H:%M}*")
    lines.append(f"{name} {px} {('('+ctf+')') if ctf else ''} · "
                 f"{len(events)} natthändelser (senaste {night_hours:g}h)")

    # ── Night recap ──────────────────────────────────────────────────────
    lines.append("\n*── Nattens läge ──*")
    if not events:
        lines.append("Lugnt: inga relevanta nyheter under natten.")
    else:
        bias, nn, nb, nbe = _bias_from_events(events)
        cat_w: dict[str, float] = {}
        for e in events:
            cat_w[e.get("category", "other")] = \
                cat_w.get(e.get("category", "other"), 0.0) + _weight(e)
        top_cat = max(cat_w, key=cat_w.get) if cat_w else "—"
        n_src = len(set(e.get("source") for e in events))
        lines.append(f"Netto-bias: *{bias}* (bull {nb} / bear {nbe}, "
                     f"styrka {nn:+.2f})")
        lines.append(f"Främsta drivkraft: {top_cat} · {n_src} källor")
        top = sorted(events, key=_weight, reverse=True)[:3]
        for e in top:
            d = {"bullish": "🟢", "bearish": "🔴"}.get(e.get("direction"), "⚪")
            t = _fmt_ts(e.get("ts", 0), tz)
            title = (e.get("title") or "").strip()[:100]
            url = e.get("url") or ""
            lines.append(f"{d} {t} {title}" + (f"\n   🔗 {url}" if url else ""))

    # ── Now / levels ─────────────────────────────────────────────────────
    lines.append("\n*── Nuläge & nivåer ──*")
    if chart is None:
        lines.append("⚠️ Ingen tillförlitlig prisdata just nu – nivåer utelämnas "
                     "(ofta normalt före Europaöppning när ETF-proxyn vilar).")
    else:
        age = getattr(chart, "last_candle_age_min", 0) or 0
        if age > 30:
            lines.append(f"ℹ️ Nivåerna är *referens från föregående session* "
                         f"(feed {age:.0f}m gammal, vilar över natten) – "
                         f"bekräfta vid Europaöppning.")
        if mtf_trends:
            arrows = {"up": "↑", "down": "↓", "sideways": "→"}
            mtf = " · ".join(f"{tf} {arrows.get(tr, '→')}"
                             for tf, tr in mtf_trends.items())
            lines.append(f"Trend MTF: {mtf} · RSI {chart.rsi:.0f} "
                         f"({chart.rsi_state()})")
        res = levels.resistances_above() if levels else []
        sup = levels.supports_below() if levels else []
        if res:
            lines.append("🎯 Motstånd: " + " · ".join(
                f"{lbl} {v:.2f}" for lbl, v in res[:3]))
        if sup:
            lines.append("🛡 Stöd: " + " · ".join(
                f"{lbl} {v:.2f}" for lbl, v in sup[:3]))
        if levels:
            extra = []
            if levels.vwap:
                extra.append(f"VWAP {levels.vwap:.2f}")
            if levels.pdh and levels.pdl:
                extra.append(f"igår {levels.pdl:.2f}–{levels.pdh:.2f}")
            if levels.day_high and levels.day_low:
                extra.append(f"natt {levels.day_low:.2f}–{levels.day_high:.2f}")
            if extra:
                lines.append("· " + " · ".join(extra))
        if cross is not None and cross.regime not in ("lugnt", "okänd"):
            lines.append(f"🌐 {cross.note}")

    # ── Day plan: intelligence-driven, ties the news picture to levels ───
    lines.append("\n*── Spelplan idag (underrättelsedriven, svensk tid) ──*")
    from .playbook import build_playbook
    lev = float(cfg.get("position.leverage", 1) or 1)
    lines.extend(build_playbook(events, chart, levels, cross, leverage=lev))

    cats = _catalysts_today(now_local, tz)
    if cats:
        lines.append("\n🗓 *Tidsmarkörer idag:*")
        for hhmm, label in cats:
            lines.append(f"  {hhmm}  {label}")

    lines.append("\n_Beslutsstöd, ej finansiell rådgivning. Handla din egen plan "
                 "och respektera stoppar._")
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
