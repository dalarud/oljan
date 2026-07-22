// Live price proxy. Prefers a REAL real-time feed (Oanda) when configured,
// and falls back to Yahoo BZ=F (delayed ~10-15 min, occasional roll gaps).
//
// To get correct, current UKOIL/Brent numbers, set these env vars in Vercel
// (a free Oanda "practice" account is enough — you don't have to trade there):
//   OANDA_API_TOKEN   – token from Oanda account → Manage API Access
//   OANDA_ENV         – "practice" (default) or "live"
//   OANDA_INSTRUMENT  – default "BCO_USD" (Brent). WTI = "WTICO_USD".
// Oanda's Brent tracks the UKOIL CFD within cents, in real time.
export const revalidate = 10;

const YAHOO_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"];

async function fromOanda() {
  const token = process.env.OANDA_API_TOKEN;
  if (!token) return null;
  const host = (process.env.OANDA_ENV === "live")
    ? "api-fxtrade.oanda.com" : "api-fxpractice.oanda.com";
  const inst = process.env.OANDA_INSTRUMENT || "BCO_USD";
  const url = `https://${host}/v3/instruments/${inst}/candles` +
    `?count=300&granularity=M5&price=M`;
  const r = await fetch(url, {
    headers: {
      Authorization: `Bearer ${token}`,
      "Accept-Datetime-Format": "UNIX",
    },
    next: { revalidate: 10 },
  });
  if (!r.ok) return null;
  const j = await r.json();
  const raw = j?.candles || [];
  const candles = [];
  for (const c of raw) {
    if (!c?.mid) continue;
    candles.push({
      t: Math.floor(parseFloat(c.time)),
      o: +c.mid.o, h: +c.mid.h, l: +c.mid.l, c: +c.mid.c,
      v: c.volume ?? 0,
    });
  }
  if (candles.length < 20) return null;
  return { candles, src: `oanda:${inst}`, realtime: true,
    fetched_at: new Date().toISOString() };
}

async function fromYahoo() {
  for (const host of YAHOO_HOSTS) {
    try {
      const url = `https://${host}/v8/finance/chart/BZ%3DF` +
        `?interval=5m&range=5d&includePrePost=false`;
      const r = await fetch(url, {
        headers: { "User-Agent": "Mozilla/5.0", Accept: "application/json" },
        next: { revalidate: 15 },
      });
      if (!r.ok) continue;
      const j = await r.json();
      const res = j?.chart?.result?.[0];
      if (!res) continue;
      const ts = res.timestamp || [];
      const q = res.indicators?.quote?.[0] || {};
      const candles = [];
      for (let i = 0; i < ts.length; i++) {
        const c = q.close?.[i];
        if (c == null) continue;
        candles.push({
          t: ts[i], o: q.open?.[i] ?? c, h: q.high?.[i] ?? c,
          l: q.low?.[i] ?? c, c, v: q.volume?.[i] ?? 0,
        });
      }
      if (candles.length > 50) {
        return { candles, src: host, realtime: false,
          fetched_at: new Date().toISOString() };
      }
    } catch {
      // try next host
    }
  }
  return null;
}

export async function GET() {
  try {
    const oanda = await fromOanda();
    if (oanda) return Response.json(oanda);
  } catch {
    // fall through to Yahoo
  }
  const yahoo = await fromYahoo();
  if (yahoo) return Response.json(yahoo);
  return Response.json({ error: "no price source available", candles: null },
    { status: 200 });
}
