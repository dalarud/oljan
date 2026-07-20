"""Candlestick chart rendering for notifications.

Uses a non-interactive matplotlib backend so it runs headless on a server.
Renders price with EMAs, Bollinger bands, marked support/resistance and a
volume panel. Failures degrade gracefully (returns None -> text-only notice).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from .indicators import ChartContext, ema, bollinger  # noqa: E402

log = logging.getLogger("oljan.charting")


def render_chart(df: pd.DataFrame, chart: ChartContext, cfg,
                 tag: str = "event") -> Optional[str]:
    try:
        return _render(df, chart, cfg, tag)
    except Exception as e:  # never let charting break a notification
        log.warning("Chart render failed: %s", e)
        return None


def _render(df: pd.DataFrame, chart: ChartContext, cfg, tag: str) -> str:
    sub = df.tail(120).copy()
    if sub.empty:
        raise ValueError("no candles to plot")

    ema_fast_p = cfg.get("indicators.ema_fast", 12)
    ema_slow_p = cfg.get("indicators.ema_slow", 26)
    bb_p = cfg.get("indicators.bb_period", 20)
    bb_std = cfg.get("indicators.bb_std", 2.0)

    ef = ema(sub["close"], ema_fast_p)
    es = ema(sub["close"], ema_slow_p)
    bb_u, bb_m, bb_l = bollinger(sub["close"], bb_p, bb_std)

    fig, (ax, axv) = plt.subplots(
        2, 1, figsize=(10, 7), gridspec_kw={"height_ratios": [3, 1]},
        sharex=True)

    x = range(len(sub))
    # candlesticks
    width = 0.6
    for i, (_, row) in enumerate(sub.iterrows()):
        up = row["close"] >= row["open"]
        color = "#26a69a" if up else "#ef5350"
        ax.plot([i, i], [row["low"], row["high"]], color=color, linewidth=0.8)
        lo = min(row["open"], row["close"])
        hi = max(row["open"], row["close"])
        ax.add_patch(plt.Rectangle((i - width / 2, lo), width, max(hi - lo, 1e-6),
                                   color=color))

    ax.plot(x, ef.values, color="#2962ff", linewidth=1.0,
            label=f"EMA{ema_fast_p}")
    ax.plot(x, es.values, color="#ff6d00", linewidth=1.0,
            label=f"EMA{ema_slow_p}")
    ax.plot(x, bb_u.values, color="#9e9e9e", linewidth=0.6, linestyle="--")
    ax.plot(x, bb_l.values, color="#9e9e9e", linewidth=0.6, linestyle="--")

    for lvl in (chart.supports or [])[:3]:
        ax.axhline(lvl, color="#00c853", linewidth=0.7, alpha=0.6)
    for lvl in (chart.resistances or [])[:3]:
        ax.axhline(lvl, color="#d50000", linewidth=0.7, alpha=0.6)

    ax.set_title(f"{chart.symbol}  {chart.price:.2f}  "
                 f"(trend {chart.trend}, RSI {chart.rsi:.0f})")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.15)

    colors = ["#26a69a" if c >= o else "#ef5350"
              for o, c in zip(sub["open"], sub["close"])]
    axv.bar(x, sub["volume"].fillna(0).values, color=colors, width=width)
    axv.set_ylabel("Vol", fontsize=8)
    axv.grid(alpha=0.15)

    fig.tight_layout()

    out_dir = Path(cfg.get("general.data_dir", "./data")) / "charts"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"{chart.symbol.replace('=', '')}_{tag}_{stamp}.png"
    fig.savefig(path, dpi=90)
    plt.close(fig)
    return str(path)
