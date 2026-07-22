"use client";
// Analysis chart: real Brent futures candles with the computed levels drawn as
// price lines and intelligence events as markers on the timeline — so the
// price reaction to each headline is visible at a glance. (The TradingView
// embed can't overlay custom data; this one can.)
import { useEffect, useRef } from "react";
import { createChart } from "lightweight-charts";

// Render candle timestamps in the trader's local wall-clock (Stockholm),
// not UTC. Lightweight-charts defaults to UTC, which made the last bar read
// ~2h "behind" the user's clock and TradingView (which shows local time).
const TZ = "Europe/Stockholm";
const hhmm = (t) => new Date(t * 1000).toLocaleTimeString("sv-SE", {
  timeZone: TZ, hour: "2-digit", minute: "2-digit",
});
const full = (t) => new Date(t * 1000).toLocaleString("sv-SE", {
  timeZone: TZ, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
});

export default function LiveChart({ candles, levels, events, height = 340 }) {
  const boxRef = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef(null);
  const priceLinesRef = useRef([]);
  const hadDataRef = useRef(false);

  useEffect(() => {
    if (!boxRef.current) return;
    const chart = createChart(boxRef.current, {
      height,
      layout: { background: { color: "#0F0F0F" }, textColor: "#9aa0aa" },
      grid: {
        vertLines: { color: "rgba(242,242,242,0.05)" },
        horzLines: { color: "rgba(242,242,242,0.05)" },
      },
      timeScale: {
        timeVisible: true, secondsVisible: false, borderColor: "#2a2d33",
        tickMarkFormatter: (t) => hhmm(t),
      },
      localization: { timeFormatter: (t) => full(t) },
      rightPriceScale: { borderColor: "#2a2d33" },
      crosshair: { mode: 0 },
    });
    const series = chart.addCandlestickSeries({
      upColor: "#26a69a", downColor: "#ef5350",
      wickUpColor: "#26a69a", wickDownColor: "#ef5350", borderVisible: false,
    });
    chartRef.current = chart;
    seriesRef.current = series;
    const ro = new ResizeObserver(() =>
      chart.applyOptions({ width: boxRef.current?.clientWidth || 600 })
    );
    ro.observe(boxRef.current);
    return () => { ro.disconnect(); chart.remove(); };
  }, [height]);

  useEffect(() => {
    const series = seriesRef.current;
    if (!series || !candles || candles.length === 0) return;
    series.setData(candles.map((c) => ({
      time: c.t, open: c.o, high: c.h, low: c.l, close: c.c,
    })));
    // Always keep the newest (delayed) bar in view. On first load fit the
    // whole series; on live updates snap to the right edge so the chart never
    // appears "stuck" on older candles.
    const tsc = chartRef.current?.timeScale();
    if (tsc) {
      if (!hadDataRef.current) { tsc.fitContent(); hadDataRef.current = true; }
      else tsc.scrollToRealTime();
    }

    // level price lines (replace on each update)
    priceLinesRef.current.forEach((pl) => { try { series.removePriceLine(pl); } catch {} });
    priceLinesRef.current = [];
    const mk = (v, color, title) => {
      if (v == null) return;
      priceLinesRef.current.push(series.createPriceLine({
        price: v, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title,
      }));
    };
    (levels?.resistance || []).forEach((r) => mk(r.v, "#ef5350", r.label));
    (levels?.support || []).forEach((s) => mk(s.v, "#26a69a", s.label));
    if (levels?.pivot != null) mk(levels.pivot, "#e2b93b", "pivot");

    // event markers snapped to the nearest candle at/before the event
    if (events && events.length && candles.length) {
      const times = candles.map((c) => c.t);
      const first = times[0];
      const snap = (ts) => {
        let lo = 0, hi = times.length - 1, ans = null;
        while (lo <= hi) {
          const mid = (lo + hi) >> 1;
          if (times[mid] <= ts) { ans = times[mid]; lo = mid + 1; } else hi = mid - 1;
        }
        return ans;
      };
      const markers = events
        .filter((e) => e.ts && e.ts >= first)
        .map((e) => ({
          time: snap(e.ts),
          position: e.dir === "bearish" ? "aboveBar" : "belowBar",
          color: e.dir === "bullish" ? "#26a69a" : e.dir === "bearish" ? "#ef5350" : "#9aa0aa",
          shape: e.dir === "bearish" ? "arrowDown" : "arrowUp",
          text: (e.title || "").slice(0, 24),
        }))
        .filter((m) => m.time != null)
        .sort((a, b) => a.time - b.time);
      series.setMarkers(markers);
    }
  }, [candles, levels, events]);

  return <div ref={boxRef} style={{ width: "100%" }} />;
}
