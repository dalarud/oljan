"""Cross-asset confirmation.

A move in oil is not self-explaining. If crude drops 1% while the dollar rips
higher and equities and gold fall too, that's a broad macro/risk move — the
oil tape is being dragged, not reacting to an oil-specific fundamental. If oil
moves while USD/equities/gold sit still, the move is genuinely oil-specific and
a news-driven read deserves more weight.

This monitor tracks a few liquid proxies (dollar, equities, gold via ETFs so
they're available on the same free feeds) and classifies the current oil move
as oil-specific, partly-macro, or macro-driven. That classification annotates
alerts and modestly tempers conviction when a fundamental oil headline's
"price confirmation" is really just the whole macro complex moving together.

Off by default (extra API budget); returns-only, so no scaling/basis concerns.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("oljan.crossasset")


@dataclass
class CrossAssetSnapshot:
    oil_ret: float                          # fractional return over the window
    proxies: dict[str, float] = field(default_factory=dict)
    regime: str = "okänd"                   # oljespecifik|delvis makro|makro-driven|lugnt
    note: str = ""

    def is_macro(self) -> bool:
        return self.regime == "makro-driven"

    def is_oil_specific(self) -> bool:
        return self.regime == "oljespecifik"


class CrossAssetMonitor:
    def __init__(self, cfg, storage, primary: str):
        self.cfg = cfg
        self.storage = storage
        self.primary = primary
        self.enabled = cfg.get("cross_asset.enabled", False)
        self.window_min = cfg.get("cross_asset.window_minutes", 60)
        self.interval = cfg.get("cross_asset.interval", "15m")
        self.lookback = cfg.get("cross_asset.lookback", "5d")
        self.oil_interval = cfg.get(
            "market_data.analysis_timeframe",
            cfg.get("market_data.intraday_interval", "5m"))
        self.move_thr = float(cfg.get("cross_asset.move_threshold_pct", 0.2)) / 100.0
        self.stale_min = cfg.get("cross_asset.stale_minutes", 90)
        # name -> ticker (ETFs so they resolve on Yahoo *and* Twelve Data)
        self.proxies: dict[str, str] = cfg.get("cross_asset.proxies", {
            "USD": "UUP", "Aktier": "SPY", "Guld": "GLD"}) or {}
        self._provider = None
        if self.enabled and not self.proxies:
            self.enabled = False

    # ------------------------------------------------------------- provider
    def _provider_lazy(self):
        if self._provider is None:
            from .providers import (ChainProvider, TwelveDataProvider,
                                    YahooChartProvider)
            chain = [YahooChartProvider()]
            td_key = self.cfg.secret("TWELVEDATA_API_KEY") if hasattr(
                self.cfg, "secret") else ""
            if td_key:
                # identity map, scaling OFF: we only need % returns.
                ident = {t: t for t in self.proxies.values()}
                chain.append(TwelveDataProvider(td_key, symbol_map=ident,
                                                scale_to_benchmark=False))
            self._provider = ChainProvider(chain)
        return self._provider

    # -------------------------------------------------------------- refresh
    def refresh(self) -> None:
        """Fetch + persist recent candles for each proxy (best-effort)."""
        if not self.enabled:
            return
        prov = self._provider_lazy()
        for ticker in self.proxies.values():
            try:
                df = prov.fetch(ticker, self.interval, self.lookback)
                if df is not None and not df.empty:
                    self.storage.upsert_candles(ticker, self.interval, df)
            except Exception as e:
                log.warning("cross-asset fetch %s failed: %s", ticker, str(e)[:80])

    # -------------------------------------------------------------- snapshot
    def snapshot(self) -> Optional[CrossAssetSnapshot]:
        if not self.enabled:
            return None
        oil_ret = self._ret(self.primary, self.oil_interval)
        if oil_ret is None:
            return None
        rets: dict[str, float] = {}
        for name, ticker in self.proxies.items():
            r = self._ret(ticker, self.interval)
            if r is not None:
                rets[name] = r

        if abs(oil_ret) < self.move_thr:
            return CrossAssetSnapshot(
                oil_ret=oil_ret, proxies=rets, regime="lugnt",
                note=f"Oljan rör sig marginellt ({oil_ret*100:+.2f}%) – ingen "
                     f"tydlig makrodrivkraft.")

        oil_up = oil_ret > 0
        drivers: list[str] = []
        usd, spy, gold = rets.get("USD"), rets.get("Aktier"), rets.get("Guld")
        # Strong dollar (UUP up) pushes oil DOWN -> explains a down move.
        if usd is not None and abs(usd) >= self.move_thr and (usd > 0) != oil_up:
            drivers.append(f"USD {usd*100:+.2f}%")
        # Equities co-move with oil on risk-on/off macro swings.
        if spy is not None and abs(spy) >= self.move_thr and (spy > 0) == oil_up:
            drivers.append(f"aktier {spy*100:+.2f}%")
        # Broad commodity/haven bid moving with oil suggests a macro complex.
        if gold is not None and abs(gold) >= self.move_thr and (gold > 0) == oil_up:
            drivers.append(f"guld {gold*100:+.2f}%")

        if not drivers:
            regime = "oljespecifik"
            note = (f"Oljan {oil_ret*100:+.2f}% medan USD/aktier/guld ligger "
                    f"stilla → oljespecifik rörelse (fundamenta väger tyngre).")
        elif len(drivers) >= 2:
            regime = "makro-driven"
            note = (f"Oljan {oil_ret*100:+.2f}% i takt med {', '.join(drivers)} "
                    f"→ sannolikt bred makrorörelse, inte oljespecifik.")
        else:
            regime = "delvis makro"
            note = (f"Oljan {oil_ret*100:+.2f}%; delvis förklarad av "
                    f"{drivers[0]}.")
        return CrossAssetSnapshot(oil_ret=oil_ret, proxies=rets, regime=regime,
                                  note=note)

    # ---------------------------------------------------------------- helper
    def _ret(self, symbol: str, interval: str) -> Optional[float]:
        try:
            df = self.storage.get_candles(symbol, interval)
        except Exception:
            return None
        if df is None or df.empty or len(df) < 2:
            return None
        last_ts = df.index[-1]
        if getattr(last_ts, "tzinfo", None) is None:
            last_ts = last_ts.tz_localize("UTC")
        if (datetime.now(timezone.utc) - last_ts) > timedelta(minutes=self.stale_min):
            return None  # too stale to compare fairly
        last = float(df["close"].iloc[-1])
        cutoff = df.index[-1] - timedelta(minutes=self.window_min)
        past = df[df.index <= cutoff]
        base = float(past["close"].iloc[-1]) if not past.empty \
            else float(df["close"].iloc[0])
        if base <= 0:
            return None
        return (last - base) / base
