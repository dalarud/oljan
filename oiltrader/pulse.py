"""Market pulse – a periodic net-bias digest across all sources.

Rather than reacting to a single item, this summarises the recent flow: how
many bullish vs. bearish stories, the net weighted bias, the dominant driver
category and the single strongest development. Sent on a schedule so you keep
situational awareness even during quiet stretches.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


def _weight(e: dict) -> float:
    return float(e.get("relevance") or 0.0) * float(e.get("substance") or 0.0)


def build_pulse(storage, hours: float, price: Optional[float],
                trend: Optional[str], name: str) -> Optional[str]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    events = [e for e in storage.recent_events(since)
              if e.get("source") != "seed"]
    px = f"{price:.2f}" if price else "n/a"
    tr = trend or "n/a"
    head = (f"🧭 *Marknadspuls* – senaste {hours:g}h\n"
            f"{name} {px} · trend {tr}")

    if not events:
        return head + "\n_Lugnt: inga relevanta nyheter i fönstret._"

    bull = [e for e in events if e.get("direction") == "bullish"]
    bear = [e for e in events if e.get("direction") == "bearish"]
    net = sum(_weight(e) for e in bull) - sum(_weight(e) for e in bear)
    total_w = sum(_weight(e) for e in events) or 1.0
    net_norm = net / total_w  # -1..+1

    if net_norm > 0.4:
        bias = "STARK HAUSSE 🟢🟢"
    elif net_norm > 0.1:
        bias = "svag hausse 🟢"
    elif net_norm < -0.4:
        bias = "STARK BAISSE 🔴🔴"
    elif net_norm < -0.1:
        bias = "svag baisse 🔴"
    else:
        bias = "neutral/blandat 🟡"

    # dominant driver category
    cat_w: dict[str, float] = {}
    for e in events:
        cat_w[e.get("category", "other")] = \
            cat_w.get(e.get("category", "other"), 0.0) + _weight(e)
    top_cat = max(cat_w, key=cat_w.get) if cat_w else "—"

    strongest = max(events, key=_weight)
    n_sources = len(set(e.get("source") for e in events))

    return (
        f"{head}\n"
        f"Netto-bias: *{bias}* (bull {len(bull)} / bear {len(bear)}, "
        f"styrka {net_norm:+.2f})\n"
        f"Främsta drivkraft: {top_cat} · {len(events)} stories, "
        f"{n_sources} källor\n"
        f"Starkast: \"{(strongest.get('title') or '')[:90]}\" "
        f"({strongest.get('direction')}, {strongest.get('source')})\n"
        f"_Översikt, ej finansiell rådgivning._"
    )
