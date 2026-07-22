"use client";
import { useEffect, useState } from "react";
import TradingViewWidget from "../components/TradingViewWidget";

const dirClass = (d) => (d === "bullish" ? "bull" : d === "bearish" ? "bear" : "neutral");
const biasLabel = (d) => (d === "bullish" ? "HAUSSE" : d === "bearish" ? "BAISSE" : "NEUTRAL");
const fmtTime = (iso) => {
  if (!iso) return "–";
  try { return new Date(iso).toLocaleString("sv-SE", { timeZone: "Europe/Stockholm" }); }
  catch { return iso; }
};
const hhmm = (ts) => {
  if (!ts) return "";
  try { return new Date(ts * 1000).toLocaleTimeString("sv-SE",
    { timeZone: "Europe/Stockholm", hour: "2-digit", minute: "2-digit" }); }
  catch { return ""; }
};

export default function Page() {
  const [s, setS] = useState(null);
  const [err, setErr] = useState(null);
  const [ago, setAgo] = useState(0);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const r = await fetch("/api/state", { cache: "no-store" });
        const data = await r.json();
        if (!alive) return;
        if (data && data.error && !data.price) setErr(data.error);
        else { setS(data); setErr(null); }
      } catch (e) { if (alive) setErr(String(e)); }
    };
    load();
    const t = setInterval(load, 60000);
    const t2 = setInterval(() => setAgo((a) => a + 1), 1000);
    return () => { alive = false; clearInterval(t); clearInterval(t2); };
  }, []);

  const stale = s && s.price_stale_min != null && s.price_stale_min > 20;
  const sym = (s && s.tv_symbol) || "TVC:UKOIL";

  return (
    <div className="wrap">
      <div className="topbar">
        <span className="brand">🛢️ Oljan</span>
        <div>
          <div className="price">{s && s.price != null ? s.price.toFixed(2) : "–"}</div>
          <div className="sub">{s ? s.instrument : "laddar…"}
            {s && s.rsi != null ? ` · RSI ${s.rsi}` : ""}
            {stale ? <span className="stale"> · est. {s.price_stale_min}m gammal</span> : ""}
          </div>
        </div>
        {s && <span className={`pill ${dirClass(s.bias)}`}>{biasLabel(s.bias)}</span>}
        {s && s.regime && <span className="pill warn">regim: {s.regime}</span>}
        {s && s.trend && Object.keys(s.trend).length > 0 && (
          <span className="pill neutral">
            MTF {Object.entries(s.trend).map(([k, v]) =>
              `${k} ${v === "up" ? "↑" : v === "down" ? "↓" : "→"}`).join(" · ")}
          </span>
        )}
        <span className="spacer" />
        <span className="sub">uppdaterad {fmtTime(s && s.updated_at)}</span>
      </div>

      {err && !s && <div className="card err">Kunde inte hämta data: {err}. (Motorn kanske inte hunnit publicera <code>state.json</code> ännu.)</div>}

      <div className="grid">
        <div className="chart"><TradingViewWidget symbol={sym} /></div>

        <div className="side">
          <div className="card">
            <h3>Dagens plan</h3>
            {s && s.plan ? (
              <ul className="plan">
                {s.plan.map((line, i) => <li key={i}>{line.replace(/\*/g, "")}</li>)}
              </ul>
            ) : <div className="sub">–</div>}
          </div>

          <div className="card">
            <h3>Nyckelnivåer</h3>
            {s && s.levels ? (
              <>
                {(s.levels.resistance || []).map((r, i) => (
                  <div className="levelrow" key={"r" + i}>
                    <span className="lvl-res">▲ motstånd · {r.label}</span><span>{r.v}</span>
                  </div>
                ))}
                {s.levels.pivot != null && (
                  <div className="levelrow"><span className="lvl-piv">◆ pivot</span><span>{s.levels.pivot}</span></div>
                )}
                {(s.levels.support || []).map((r, i) => (
                  <div className="levelrow" key={"s" + i}>
                    <span className="lvl-sup">▼ stöd · {r.label}</span><span>{r.v}</span>
                  </div>
                ))}
                <div className="sub" style={{ marginTop: 8 }}>
                  Läs exakta nivåer på charten ovan (live UKOIL). Dessa är motorns beräknade nivåer.
                </div>
              </>
            ) : <div className="sub">–</div>}
          </div>
        </div>

        <div className="card full">
          <h3>Underrättelseflöde (senaste 24h)</h3>
          {s && s.events && s.events.length > 0 ? s.events.map((e, i) => (
            <div className="ev" key={i}>
              <div className="t"><span className={`dot ${dirClass(e.dir)}`} />
                {e.url ? <a href={e.url} target="_blank" rel="noopener noreferrer">{e.title}</a> : e.title}
              </div>
              <div className="m">{hhmm(e.ts)} · {e.cat} · rel {e.rel} · substans {e.sub}</div>
            </div>
          )) : <div className="sub">Inga relevanta händelser i fönstret.</div>}
        </div>

        <div className="card">
          <h3>Marknadspuls</h3>
          <p className="pre">{(s && s.pulse) ? s.pulse.replace(/\*/g, "") : "–"}</p>
        </div>

        <div className="card">
          <h3>Träffsäkerhet</h3>
          <p className="pre">{(s && s.scorecard) ? s.scorecard.replace(/\*/g, "") : "Bygger upp track record…"}</p>
        </div>
      </div>

      <div className="foot">
        Oljan · beslutsstöd, ej finansiell rådgivning · chart av TradingView (live UKOIL) ·
        analys/underrättelser av motorn (pris = skalad estimat, kalibrerad).
      </div>
    </div>
  );
}
