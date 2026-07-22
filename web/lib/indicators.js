// Client-side indicators computed on REAL Brent futures candles (from
// /api/price). Everything here is basis-correct by construction — no scaling.

export function rsi(closes, period = 14) {
  if (!closes || closes.length < period + 1) return null;
  let gain = 0, loss = 0;
  for (let i = 1; i <= period; i++) {
    const d = closes[i] - closes[i - 1];
    if (d > 0) gain += d; else loss -= d;
  }
  let ag = gain / period, al = loss / period;
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1];
    ag = (ag * (period - 1) + Math.max(d, 0)) / period;
    al = (al * (period - 1) + Math.max(-d, 0)) / period;
  }
  if (al === 0) return 100;
  return 100 - 100 / (1 + ag / al);
}

// Full RSI series (Wilder) — needed for adaptive thresholds.
export function rsiSeries(closes, period = 14) {
  if (!closes || closes.length < period + 1) return [];
  const out = new Array(period).fill(null);
  let gain = 0, loss = 0;
  for (let i = 1; i <= period; i++) {
    const d = closes[i] - closes[i - 1];
    if (d > 0) gain += d; else loss -= d;
  }
  let ag = gain / period, al = loss / period;
  out.push(al === 0 ? 100 : 100 - 100 / (1 + ag / al));
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1];
    ag = (ag * (period - 1) + Math.max(d, 0)) / period;
    al = (al * (period - 1) + Math.max(-d, 0)) / period;
    out.push(al === 0 ? 100 : 100 - 100 / (1 + ag / al));
  }
  return out;
}

// Adaptive oversold/overbought lines from the recent RSI distribution — the
// same rule as the engine (backtest.py): a fixed 30/70 only ever fires
// counter-trend in a trending tape, which lost money. Clamped to sane bands.
export function rsiBands(closes, period = 14) {
  const ser = rsiSeries(closes, period).filter((x) => x != null);
  if (ser.length < 50) return { os: 30, ob: 70 };
  const recent = ser.slice(-200).sort((a, b) => a - b);
  const q = (p) => recent[Math.min(recent.length - 1, Math.floor(p * recent.length))];
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  return { os: clamp(q(0.20), 25, 45), ob: clamp(q(0.80), 55, 75) };
}

export function ema(closes, period) {
  if (!closes || closes.length < period) return null;
  const k = 2 / (period + 1);
  let e = closes.slice(0, period).reduce((a, b) => a + b, 0) / period;
  for (let i = period; i < closes.length; i++) e = closes[i] * k + e * (1 - k);
  return e;
}

export function atr(candles, period = 14) {
  if (!candles || candles.length < period + 1) return null;
  const trs = [];
  for (let i = 1; i < candles.length; i++) {
    trs.push(Math.max(
      candles[i].h - candles[i].l,
      Math.abs(candles[i].h - candles[i - 1].c),
      Math.abs(candles[i].l - candles[i - 1].c)
    ));
  }
  let a = trs.slice(0, period).reduce((x, y) => x + y, 0) / period;
  for (let i = period; i < trs.length; i++) a = (a * (period - 1) + trs[i]) / period;
  return a;
}

const utcDate = (t) => new Date(t * 1000).toISOString().slice(0, 10);

// Canonical levels straight from the real candles: day H/L, prior-day H/L/C,
// session VWAP, round numbers. Labeled + split into resistance/support.
export function computeLevels(candles) {
  if (!candles || candles.length < 30) return null;
  const price = candles[candles.length - 1].c;
  const today = utcDate(candles[candles.length - 1].t);
  const dayC = candles.filter((c) => utcDate(c.t) === today);
  const days = [...new Set(candles.map((c) => utcDate(c.t)))];
  const prevDay = days.length > 1 ? days[days.length - 2] : null;
  const prevC = prevDay ? candles.filter((c) => utcDate(c.t) === prevDay) : [];

  const dayHigh = dayC.length ? Math.max(...dayC.map((c) => c.h)) : null;
  const dayLow = dayC.length ? Math.min(...dayC.map((c) => c.l)) : null;
  const pdh = prevC.length ? Math.max(...prevC.map((c) => c.h)) : null;
  const pdl = prevC.length ? Math.min(...prevC.map((c) => c.l)) : null;
  const pdc = prevC.length ? prevC[prevC.length - 1].c : null;

  let vwap = null;
  const vol = dayC.reduce((s, c) => s + (c.v || 0), 0);
  if (vol > 0) {
    vwap = dayC.reduce((s, c) => s + ((c.h + c.l + c.c) / 3) * (c.v || 0), 0) / vol;
  }

  const rUp1 = Math.ceil(price);
  const rUp05 = Math.floor(price) + 0.5 > price ? Math.floor(price) + 0.5 : null;
  const rDn1 = Math.floor(price);
  const rDn05 = Math.ceil(price) - 0.5 < price ? Math.ceil(price) - 0.5 : null;

  const cands = [
    ["dagshögsta", dayHigh], ["dagslägsta", dayLow],
    ["PDH", pdh], ["PDL", pdl], ["PDC", pdc], ["VWAP", vwap],
    ["rund", rUp1], ["rund", rUp05], ["rund", rDn1], ["rund", rDn05],
  ].filter(([, v]) => v != null && isFinite(v));

  const dedupe = (arr) => {
    const out = [];
    for (const [l, v] of arr) {
      if (out.some(([, w]) => Math.abs(v - w) / Math.max(w, 1e-9) < 0.001)) continue;
      out.push([l, v]);
    }
    return out;
  };
  const resistance = dedupe(
    cands.filter(([, v]) => v > price * 1.0005).sort((a, b) => a[1] - b[1])
  ).slice(0, 3).map(([label, v]) => ({ label, v: +v.toFixed(2) }));
  const support = dedupe(
    cands.filter(([, v]) => v < price * 0.9995).sort((a, b) => b[1] - a[1])
  ).slice(0, 3).map(([label, v]) => ({ label, v: +v.toFixed(2) }));

  return {
    price: +price.toFixed(2), dayHigh, dayLow, pdh, pdl, pdc,
    vwap: vwap != null ? +vwap.toFixed(2) : null,
    pivot: vwap != null ? +vwap.toFixed(2) : (pdc != null ? +pdc.toFixed(2) : null),
    resistance, support,
  };
}
