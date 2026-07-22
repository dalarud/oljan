"""Backtester for the mean-reversion RSI signal.

The live panel tells the trader to fade a "softening RSI at a support level".
Two real trades on that rule lost. Before rewriting the rule on intuition we
measure it: this module replays stored Brent candles and compares the current
naive signal against progressively-filtered variants, so the live rules can be
rebuilt on evidence.

Why stored (possibly ETF-scaled) candles are valid here: RSI, ATR-based R
multiples and percentage returns are all scale-invariant. A constant scale
factor on price leaves every statistic this module reports unchanged, so the
edge numbers hold even if the absolute basis differs from the screen.

Variants
--------
S0  naive        current live logic: RSI reclaim across a fixed 30/70 line,
                 near any level, either direction, no regime filter.
S1  regime-gated S0 but only *with-trend* pullback reversions (buy dips in an
                 uptrend, sell rips in a downtrend; both sides only in a range).
                 Counter-trend fades are dropped.
S2  confirmed    S1 plus (a) adaptive RSI thresholds that follow the recent RSI
                 range instead of a fixed 30/70, and (b) a rejection candle on
                 the trigger bar (long wick against the entry, close snapping
                 back). This is the "don't fade until price confirms" rule.

Exit model is IDENTICAL across variants so differences come only from entry
filtering: ATR-based stop and target, with a max hold. A second, exit-free
measure (signed forward return at a fixed horizon) is reported alongside so no
conclusion depends on the exit tuning.

Run: python -m oiltrader.backtest [--symbol BZ=F] [--interval 5m]
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from .indicators import atr as atr_series
from .indicators import ema, rsi
from .storage import Storage


# --------------------------------------------------------------------- config
@dataclass
class StratCfg:
    name: str
    regime_gated: bool = False       # only with-trend reversions
    adaptive_rsi: bool = False       # thresholds follow recent RSI range
    require_rejection: bool = False  # rejection candle on the trigger bar


@dataclass
class ExitCfg:
    stop_atr: float = 1.0            # stop distance in ATRs
    target_atr: float = 1.5          # target distance in ATRs (=> 1.5R here)
    max_bars: int = 24               # hard time exit (24 * 5m = 2h)
    horizon_bars: int = 12           # exit-free signed-return horizon (1h on 5m)


# ------------------------------------------------------------------ utilities
def _trend(ema_fast: pd.Series, ema_slow: pd.Series) -> np.ndarray:
    """up / down / range per bar, matching the live engine's rule."""
    ef = ema_fast.values
    es = ema_slow.values
    n = len(ef)
    out = np.empty(n, dtype=object)
    for i in range(n):
        j = max(0, i - 5)
        slope = ef[i] - ef[j]
        if ef[i] > es[i] and slope > 0:
            out[i] = "up"
        elif ef[i] < es[i] and slope < 0:
            out[i] = "down"
        else:
            out[i] = "range"
    return out


def _swing_levels(df: pd.DataFrame, width: int = 3):
    """Confirmed swing highs/lows with the bar index at which each becomes
    *known* (pivot index + width). No look-ahead: a pivot at j is usable only
    from bar j+width onward."""
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    res, sup = [], []   # (known_from_index, level_value)
    for i in range(width, n - width):
        wh = highs[i - width:i + width + 1]
        wl = lows[i - width:i + width + 1]
        if highs[i] == wh.max():
            res.append((i + width, float(highs[i])))
        if lows[i] == wl.min():
            sup.append((i + width, float(lows[i])))
    return res, sup


def _near_level(price: float, levels, known_upto: int, band: float,
                below: bool) -> Optional[float]:
    """Nearest known level on the requested side within `band` (fractional)."""
    best = None
    best_d = band
    for known_from, v in levels:
        if known_from > known_upto:
            continue
        if below and v >= price:
            continue
        if not below and v <= price:
            continue
        d = abs(price - v) / price
        if d <= best_d:
            best_d = d
            best = v
    return best


# ------------------------------------------------------------------- simulate
def simulate(df: pd.DataFrame, strat: StratCfg, ex: ExitCfg,
             rsi_p: int = 14, atr_p: int = 14,
             ob: float = 70.0, os_: float = 30.0,
             near_band: float = 0.007) -> dict:
    """Replay one strategy over a candle frame; return a stats dict."""
    close = df["close"]
    r = rsi(close, rsi_p)
    a = atr_series(df, atr_p)
    ef = ema(close, 12)
    es = ema(close, 26)
    trend = _trend(ef, es)
    res_lv, sup_lv = _swing_levels(df, width=3)

    rv = r.values
    av = a.values
    hi = df["high"].values
    lo = df["low"].values
    cl = close.values
    n = len(df)

    # adaptive thresholds: rolling RSI percentiles, clamped to sane bands so a
    # persistent trend can't push the "oversold" line to absurd extremes.
    if strat.adaptive_rsi:
        rs = pd.Series(rv)
        os_dyn = rs.rolling(200, min_periods=50).quantile(0.20) \
            .clip(lower=25, upper=45).values
        ob_dyn = rs.rolling(200, min_periods=50).quantile(0.80) \
            .clip(lower=55, upper=75).values
    else:
        os_dyn = np.full(n, os_)
        ob_dyn = np.full(n, ob)

    trades = []
    warm = max(rsi_p, atr_p, 50) + 5
    i = warm
    while i < n - 1:
        prev_rsi, cur_rsi = rv[i - 1], rv[i]
        atr_now = av[i]
        price = cl[i]
        if not np.isfinite(atr_now) or atr_now <= 0 or not np.isfinite(cur_rsi):
            i += 1
            continue

        side = None
        os_t, ob_t = os_dyn[i], ob_dyn[i]
        # oversold reclaim -> long
        if prev_rsi <= os_t < cur_rsi:
            lvl = _near_level(price, sup_lv, i, near_band, below=True)
            if lvl is not None:
                side = "long"
        # overbought reclaim -> short
        elif prev_rsi >= ob_t > cur_rsi:
            lvl = _near_level(price, res_lv, i, near_band, below=False)
            if lvl is not None:
                side = "short"
        if side is None:
            i += 1
            continue

        tr = trend[i]
        with_trend = (side == "long" and tr in ("up", "range")) or \
                     (side == "short" and tr in ("down", "range"))
        if strat.regime_gated and not with_trend:
            i += 1
            continue

        if strat.require_rejection:
            rng = hi[i] - lo[i]
            if rng <= 0:
                i += 1
                continue
            if side == "long":
                # lower wick rejected: close in the upper 40% of the bar
                if (cl[i] - lo[i]) / rng < 0.6:
                    i += 1
                    continue
            else:
                if (hi[i] - cl[i]) / rng < 0.6:
                    i += 1
                    continue

        # ---- exit simulation (identical across variants) -----------------
        entry = price
        if side == "long":
            stop = entry - ex.stop_atr * atr_now
            target = entry + ex.target_atr * atr_now
        else:
            stop = entry + ex.stop_atr * atr_now
            target = entry - ex.target_atr * atr_now
        risk = abs(entry - stop)

        outcome_r = None
        for k in range(i + 1, min(i + 1 + ex.max_bars, n)):
            h, l = hi[k], lo[k]
            if side == "long":
                hit_stop = l <= stop
                hit_tgt = h >= target
            else:
                hit_stop = h >= stop
                hit_tgt = l <= target
            if hit_stop and hit_tgt:
                outcome_r = -1.0          # same-bar ambiguity -> assume stop
                break
            if hit_stop:
                outcome_r = -abs(entry - stop) / risk
                break
            if hit_tgt:
                outcome_r = abs(target - entry) / risk
                break
        exit_idx = min(i + ex.max_bars, n - 1)
        if outcome_r is None:             # time exit at market
            ex_price = cl[exit_idx]
            outcome_r = ((ex_price - entry) if side == "long"
                         else (entry - ex_price)) / risk

        # exit-free signed forward return at fixed horizon
        h_idx = min(i + ex.horizon_bars, n - 1)
        fwd = (cl[h_idx] - entry) / entry
        signed_fwd = fwd if side == "long" else -fwd

        trades.append({
            "idx": i, "side": side, "trend": tr, "with_trend": with_trend,
            "r": outcome_r, "signed_fwd": signed_fwd,
        })
        # advance past this trade's exit to avoid overlapping entries
        i = max(i + 1, exit_idx)

    return _stats(strat.name, trades)


def _stats(name: str, trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"name": name, "n": 0}
    rs = np.array([t["r"] for t in trades])
    fwd = np.array([t["signed_fwd"] for t in trades])
    wins = rs[rs > 0]
    losses = rs[rs < 0]
    win_rate = len(wins) / n
    expectancy = rs.mean()
    gross_win = wins.sum()
    gross_loss = -losses.sum()
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    # worst losing streak
    streak = worst = 0
    for t in trades:
        if t["r"] < 0:
            streak += 1
            worst = max(worst, streak)
        else:
            streak = 0

    def _sub(pred):
        sub = [t for t in trades if pred(t)]
        if not sub:
            return None
        srs = np.array([t["r"] for t in sub])
        return {"n": len(sub), "win": float((srs > 0).mean()),
                "exp": float(srs.mean())}

    return {
        "name": name, "n": n, "win_rate": win_rate, "expectancy_r": expectancy,
        "profit_factor": pf, "total_r": rs.sum(), "worst_streak": worst,
        "fwd_mean_pct": fwd.mean() * 100, "fwd_win": float((fwd > 0).mean()),
        "long": _sub(lambda t: t["side"] == "long"),
        "short": _sub(lambda t: t["side"] == "short"),
        "with_trend": _sub(lambda t: t["with_trend"]),
        "counter_trend": _sub(lambda t: not t["with_trend"]),
    }


# ---------------------------------------------------------------------- report
def _fmt(s: dict) -> str:
    if s.get("n", 0) == 0:
        return f"  {s['name']:<16} — inga trades (villkoren slog aldrig in)"
    pf = s["profit_factor"]
    pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
    lines = [
        f"  {s['name']:<16} n={s['n']:<4} "
        f"träffsäkerhet={s['win_rate']*100:4.1f}%  "
        f"förväntan={s['expectancy_r']:+.3f}R  PF={pf_s:<5} "
        f"summa={s['total_r']:+.1f}R  värsta_svit={s['worst_streak']}",
        f"  {'':<16} exit-fritt: snitt fwd-avk={s['fwd_mean_pct']:+.3f}%  "
        f"riktningsträff={s['fwd_win']*100:4.1f}%",
    ]
    for key, label in (("with_trend", "med trend"),
                       ("counter_trend", "mot trend"),
                       ("long", "long"), ("short", "short")):
        sub = s.get(key)
        if sub:
            lines.append(
                f"  {'':<16}   {label:<10} n={sub['n']:<4} "
                f"träff={sub['win']*100:4.1f}%  förväntan={sub['exp']:+.3f}R")
    return "\n".join(lines)


def run(symbol: str = "BZ=F", interval: str = "5m",
        db: str = "data/oljan.db") -> None:
    st = Storage(db)
    df = st.get_candles(symbol, interval)
    st.close()
    if df.empty or len(df) < 100:
        print(f"Otillräcklig data för {symbol} {interval} "
              f"({len(df)} candles).")
        return

    span = f"{df.index[0].date()} .. {df.index[-1].date()}"
    print(f"\n=== Backtest {symbol} {interval} — {len(df)} candles ({span}) ===")
    print("RSI/ATR-R/%-avkastning är skalinvarianta → giltigt på lagrad "
          "(ev. ETF-skalad) data.\n")

    ex = ExitCfg()
    variants = [
        StratCfg("S0 naiv"),
        StratCfg("S1 regim", regime_gated=True),
        StratCfg("S2 bekräftad", regime_gated=True, adaptive_rsi=True,
                 require_rejection=True),
    ]
    results = []
    for v in variants:
        s = simulate(df, v, ex)
        results.append(s)
        print(_fmt(s))
        print()

    print(f"Exit-modell: stop {ex.stop_atr}·ATR, mål {ex.target_atr}·ATR "
          f"(~{ex.target_atr/ex.stop_atr:.1f}R), max {ex.max_bars} barer. "
          f"Fwd-horisont {ex.horizon_bars} barer.")
    print("Träffsäkerhet = andel vinnande trades. Förväntan = snitt-R per "
          "trade (positiv = edge). PF = bruttovinst/bruttoförlust.")
    return results


def main() -> None:
    p = argparse.ArgumentParser(description="Backtest the mean-reversion signal")
    p.add_argument("--symbol", default="BZ=F")
    p.add_argument("--interval", default="5m")
    p.add_argument("--db", default="data/oljan.db")
    args = p.parse_args()
    if not Path(args.db).exists():
        print(f"Databas saknas: {args.db}")
        return
    run(args.symbol, args.interval, args.db)


if __name__ == "__main__":
    main()
