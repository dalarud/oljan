// Live Brent futures (Yahoo BZ=F) proxied through Vercel's servers.
// Vercel's IPs are not the rate-limited sandbox IP, and the 15s route cache
// means many viewers/polls collapse into ~4 upstream requests per minute.
export const revalidate = 15;

const HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"];

export async function GET() {
  for (const host of HOSTS) {
    try {
      const url =
        `https://${host}/v8/finance/chart/BZ%3DF?interval=5m&range=5d&includePrePost=false`;
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
          t: ts[i],
          o: q.open?.[i] ?? c,
          h: q.high?.[i] ?? c,
          l: q.low?.[i] ?? c,
          c,
          v: q.volume?.[i] ?? 0,
        });
      }
      if (candles.length > 50) {
        return Response.json({
          candles,
          src: host,
          fetched_at: new Date().toISOString(),
        });
      }
    } catch {
      // try next host
    }
  }
  return Response.json({ error: "yahoo unavailable", candles: null }, { status: 200 });
}
