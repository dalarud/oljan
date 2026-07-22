"use client";
// Leverage-aware position calculator: turns "stop >1 ATR, small size" into
// numbers. Pure client-side; nothing is sent anywhere.
import { useState } from "react";

export default function RiskCalc({ price, atrVal, leverage = 10, target }) {
  const [entry, setEntry] = useState("");
  const [stop, setStop] = useState("");
  const [account, setAccount] = useState("");
  const [riskPct, setRiskPct] = useState("1");
  const [lev, setLev] = useState(String(leverage));

  const e = parseFloat(entry) || price || 0;
  const s = parseFloat(stop) || 0;
  const acc = parseFloat(account) || 0;
  const rp = Math.max(parseFloat(riskPct) || 1, 0.1);
  const L = Math.max(parseFloat(lev) || 1, 1);

  const dist = e && s ? Math.abs(e - s) : 0;
  const movePct = e ? (dist / e) * 100 : 0;
  const marginPct = movePct * L;
  const atrMult = atrVal ? dist / atrVal : null;
  const riskAmt = acc * (rp / 100);
  const units = dist > 0 ? riskAmt / dist : 0;
  const notional = units * e;
  const margin = L > 0 ? notional / L : 0;
  const tgt = target ?? null;
  const rr = tgt && dist > 0 ? Math.abs(tgt - e) / dist : null;

  const warn = atrMult != null && atrMult < 1;

  return (
    <div className="card">
      <h3>Risk & position (x{L})</h3>
      <div className="calcgrid">
        <label>Entry <input value={entry} onChange={(x) => setEntry(x.target.value)}
          placeholder={price ? price.toFixed(2) : "—"} inputMode="decimal" /></label>
        <label>Stop <input value={stop} onChange={(x) => setStop(x.target.value)}
          placeholder="t.ex. 91.80" inputMode="decimal" /></label>
        <label>Konto <input value={account} onChange={(x) => setAccount(x.target.value)}
          placeholder="t.ex. 10000" inputMode="decimal" /></label>
        <label>Risk % <input value={riskPct} onChange={(x) => setRiskPct(x.target.value)}
          inputMode="decimal" /></label>
        <label>Hävstång <input value={lev} onChange={(x) => setLev(x.target.value)}
          inputMode="decimal" /></label>
      </div>
      {dist > 0 ? (
        <div style={{ marginTop: 10 }}>
          <div className="levelrow"><span>Stoppavstånd</span>
            <span>{dist.toFixed(2)} ({movePct.toFixed(2)} %{atrMult != null ? ` · ${atrMult.toFixed(1)} ATR` : ""})</span></div>
          <div className="levelrow"><span>Marginalpåverkan vid stopp</span>
            <span className={marginPct > 15 ? "lvl-res" : ""}>{marginPct.toFixed(1)} %</span></div>
          {acc > 0 && (<>
            <div className="levelrow"><span>Storlek för {rp}% kontorisk</span>
              <span>{units.toFixed(1)} enheter (~{notional.toFixed(0)})</span></div>
            <div className="levelrow"><span>Marginalkrav</span><span>~{margin.toFixed(0)}</span></div>
          </>)}
          {rr != null && (
            <div className="levelrow"><span>R/R mot närmaste mål {tgt.toFixed(2)}</span>
              <span className={rr < 1 ? "lvl-res" : "lvl-sup"}>{rr.toFixed(2)}</span></div>
          )}
          {warn && <div className="sub stale" style={{ marginTop: 6 }}>
            ⚠️ Stopp &lt; 1 ATR — inom bruset, hög risk att stoppas ur på slump.</div>}
        </div>
      ) : (
        <div className="sub" style={{ marginTop: 8 }}>Fyll i entry + stop.</div>
      )}
    </div>
  );
}
