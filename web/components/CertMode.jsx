"use client";
// Certifikat-läge: helps trade Avanza BULL/BEAR OLJA X{N} constant-leverage
// certificates correctly. Those track the Brent *future* (not UKOIL spot) and
// carry daily-leverage decay + an intraday reset barrier, so this card:
//   • computes on the future basis (spliced BZ=F, no UKOIL calibration offset);
//   • shows the expected ≈factor × underlying move (today and since entry);
//   • marks cert break-even = underlying back at entry;
//   • shows distance to the ≈(100/factor)% intraday reset barrier;
//   • warns that these are intraday-only (decay/financing over days).
// Everything is approximate and labelled "≈"; no false precision.
import { useState } from "react";

const utcDate = (t) => new Date(t * 1000).toISOString().slice(0, 10);

export default function CertMode({ futCandles }) {
  const [side, setSide] = useState("BEAR");
  const [factor, setFactor] = useState("10");
  const [entry, setEntry] = useState("");

  const ready = futCandles && futCandles.length >= 2;
  const price = ready ? futCandles[futCandles.length - 1].c : null;
  const f = Math.max(parseFloat(factor) || 10, 1);
  const dir = side === "BULL" ? 1 : -1;

  let todayPct = null, dayOpen = null;
  if (ready) {
    const today = utcDate(futCandles[futCandles.length - 1].t);
    const tc = futCandles.filter((c) => utcDate(c.t) === today);
    dayOpen = tc.length ? tc[0].o : futCandles[0].o;
    if (dayOpen) todayPct = ((price - dayOpen) / dayOpen) * 100;
  }
  const certToday = todayPct != null ? f * todayPct * dir : null;

  const e = parseFloat(entry) || null;
  const sincePct = e && price ? ((price - e) / e) * 100 : null;
  const certSince = sincePct != null ? f * sincePct * dir : null;

  const adverse = 1 / f;                        // ~10% for X10
  const barrier = price != null
    ? (side === "BULL" ? price * (1 - adverse) : price * (1 + adverse)) : null;

  const sign = (v) => (v == null ? "–" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`);
  const cls = (v) => (v == null ? "" : v >= 0 ? "good" : "bad");

  return (
    <section className="ol-card ol-cert">
      <div className="ol-cardhead">
        <span className="ol-cardtitle">Certifikat-läge</span>
        <span className="ol-cardsub">Avanza BULL/BEAR · underliggande Brent-termin</span>
      </div>

      <div className="ol-cert-controls">
        <div className="ol-cert-side">
          <button className={side === "BULL" ? "on bull" : ""} onClick={() => setSide("BULL")}>BULL</button>
          <button className={side === "BEAR" ? "on bear" : ""} onClick={() => setSide("BEAR")}>BEAR</button>
        </div>
        <label className="ol-cert-field">Hävstång ×
          <input inputMode="decimal" value={factor} onChange={(e2) => setFactor(e2.target.value)} />
        </label>
        <label className="ol-cert-field">Din ingång (termin)
          <input inputMode="decimal" placeholder={price ? price.toFixed(2) : "—"}
            value={entry} onChange={(e2) => setEntry(e2.target.value)} />
        </label>
      </div>

      {ready ? (
        <div className="ol-cert-out">
          <div className="ol-result-row">
            <span className="ol-result-label">Underliggande (termin, BZ=F)</span>
            <span className="ol-result-val">{price.toFixed(2)}</span>
          </div>
          <div className="ol-result-row">
            <span className="ol-result-label">Idag: termin {sign(todayPct)} → cert ≈</span>
            <span className={`ol-result-val ${cls(certToday)}`}>{sign(certToday)}</span>
          </div>
          {certSince != null && (
            <>
              <div className="ol-result-row">
                <span className="ol-result-label">Sedan din ingång {e.toFixed(2)}: termin {sign(sincePct)} → cert ≈</span>
                <span className={`ol-result-val ${cls(certSince)}`}>{sign(certSince)}</span>
              </div>
              <div className="ol-cert-note">
                Cert break-even ≈ när terminen är åter på {e.toFixed(2)} — inte när
                din UKOIL-graf är det.
              </div>
            </>
          )}
          <div className="ol-result-row">
            <span className="ol-result-label">≈{(adverse * 100).toFixed(0)}%-reset ({side === "BULL" ? "ned" : "upp"})</span>
            <span className="ol-result-val bad">{barrier.toFixed(2)}</span>
          </div>
          <div className="ol-cert-warn">
            ⚠️ Endast intraday. Daglig hävstång ger volatilitetsdecay + finansiering
            över natten/rullning — sitt inte kvar över dagar. En rörelse mot dig till
            ~{barrier.toFixed(2)} nollställer i princip certet (intraday-reset).
          </div>
          <div className="ol-cert-foot">
            Räknat på terminsbasen (splice, utan UKOIL-kalibrering) — det certet faktiskt
            följer. ≈ approximation; certet i SEK kan även röras av USD/SEK om ej valutasäkrat.
          </div>
        </div>
      ) : <div className="ol-empty">Väntar på terminsdata…</div>}
    </section>
  );
}
