"""Entry point.

    python -m oiltrader --config config.yaml          # run 24/7
    python -m oiltrader --config config.yaml --once    # single pass (cron)
    python -m oiltrader --config config.yaml --selftest  # sanity check
"""
from __future__ import annotations

import argparse
import sys

from .config import Config
from .logging_setup import setup_logging


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="oiltrader",
                                     description="Oljan – crude oil watch & analysis engine")
    parser.add_argument("--config", "-c", default="config.yaml",
                        help="path to config YAML (default: config.yaml)")
    parser.add_argument("--once", action="store_true",
                        help="run a single news+market pass and exit (testing)")
    parser.add_argument("--cron", action="store_true",
                        help="one full scheduled pass (news+setups+morning+pulse)"
                             " and exit; for a scheduled runner with a persisted DB")
    parser.add_argument("--selftest", action="store_true",
                        help="verify data access + notification channel and exit")
    args = parser.parse_args(argv)

    cfg = Config.load(args.config)
    log = setup_logging(
        level=cfg.get("general.log_level", "INFO"),
        log_dir=str(cfg.data_dir / "logs"),
    )

    if args.selftest:
        return _selftest(cfg, log)

    from .daemon import Daemon
    daemon = Daemon(cfg)
    try:
        if args.cron:
            daemon.run_cron()
        elif args.once:
            daemon.run_once()
        else:
            daemon.run()
    except KeyboardInterrupt:
        log.info("Interrupted by user.")
    return 0


def d_intervals(cfg) -> list[str]:
    tfs = cfg.get("market_data.timeframes", None)
    if tfs:
        return [t["interval"] for t in tfs]
    return [cfg.get("market_data.intraday_interval", "15m")]


def _selftest(cfg, log) -> int:
    ok = True
    log.info("Selftest: fetching market data (timeframes=%s) ...",
             ", ".join(d_intervals(cfg)))
    from .daemon import Daemon
    from .indicators import compute
    d = Daemon(cfg)
    for sym in d.symbols:
        tf_data = d.market.refresh_all(sym)
        got_any = False
        for interval, df in tf_data.items():
            if df is None or df.empty:
                log.warning("  %s [%s]: NO DATA", sym, interval)
                continue
            got_any = True
            log.info("  %s [%s]: %d candles, last close %.2f", sym, interval,
                     len(df), df["close"].iloc[-1])
            try:
                ctx = compute(df, sym, cfg)
                log.info("     trend=%s RSI=%.0f support=%s resistance=%s",
                         ctx.trend, ctx.rsi, ctx.nearest_support,
                         ctx.nearest_resistance)
            except Exception as e:
                log.error("     indicator computation failed: %s", e)
                ok = False
        if not got_any:
            log.error("  %s: NO DATA on any timeframe", sym)
            ok = False

    log.info("Selftest: collectors ...")
    for c in d.collectors:
        try:
            items = list(c.collect())
            log.info("  %s: %d items", c.name, len(items))
        except Exception as e:
            log.error("  %s: FAILED %s", c.name, e)

    log.info("Selftest: notification channel (%s) ...", d.notifier.channel)
    d.notifier.send_text("✅ Oljan selftest: notiskanalen fungerar.")

    log.info("Selftest %s", "PASSED" if ok else "COMPLETED WITH ERRORS")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
