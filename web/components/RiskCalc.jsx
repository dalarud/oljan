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
  const ready = dist > 0;

  const field = (label, val, set, ph, step) => (
    <label className="ol-input-label">{label}
      <input className="ol-input" type="number" step={step} value={val}
        placeholder={ph} inputMode="decimal"
        onChange={(x) => set(x.target.value)} />
    </label>
  );
  const row = (label, value, cls) => (
    <div className="ol-result-row">
      <span className="ol-result-label">{label}</span>
      <span className={`ol-result-val${cls ? " " + cls : ""}`}>{value}</span>
    </div>
  );

  return (
    <section className="ol-card">
      <div className="ol-cardhead">
        <span className="ol-cardtitle">Riskkalkylator</span>
        <span className="ol-cardsub">Beräknas lokalt · x{L}</span>
      </div>
      <div className="ol-risk">
        <div className="ol-risk-inputs">
          {field("Entry", entry, setEntry, price ? price.toFixed(2) : "—", "0.01")}
          {field("Stopp", stop, setStop, "t.ex. 91.80", "0.01")}
          {field("Konto (USD)", account, setAccount, "t.ex. 10000", "100")}
          {field("Risk %", riskPct, setRiskPct, "1", "0.1")}
          {field("Hävstång", lev, setLev, "10", "1")}
        </div>
        <div className="ol-risk-results">
          {ready ? (
            <>
              {row("Avstånd (ATR)", atrMult != null ? `${atrMult.toFixed(2)}×` : "—", warn ? "bad" : "")}
              {row("Stoppavstånd", `${dist.toFixed(2)} (${movePct.toFixed(2)} %)`)}
              {row("Marginalpåverkan", `${marginPct.toFixed(1)} %`, marginPct > 15 ? "bad" : "")}
              {acc > 0 && row("Positionsstorlek", `${units.toFixed(1)} enh · $${notional.toFixed(0)}`)}
              {acc > 0 && row("Marginalkrav", `$${margin.toFixed(0)}`)}
              {rr != null && row(`Risk/Reward → ${tgt.toFixed(2)}`, `${rr.toFixed(2)} : 1`, rr >= 1 ? "good" : "bad")}
              {warn && (
                <div className="ol-risk-warn">
                  Stoppen är tajtare än 1 ATR — förhöjd risk för brus-stopp.
                </div>
              )}
            </>
          ) : (
            <div className="ol-result-label">Fyll i entry + stopp för att räkna.</div>
          )}
        </div>
      </div>
    </section>
  );
}
