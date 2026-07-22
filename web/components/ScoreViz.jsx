"use client";
// Visual track record: each scored alert as a dot (hit/miss) plus precision
// per conviction band — makes the engine's self-evaluation glanceable.

const BANDS = [[0, 39, "0–39"], [40, 59, "40–59"], [60, 79, "60–79"], [80, 100, "80–100"]];

export default function ScoreViz({ alerts, fallbackText }) {
  const scored = (alerts || []).filter((a) => a.correct != null);
  if (scored.length === 0) {
    return (
      <div className="card">
        <h3>Träffsäkerhet</h3>
        <p className="pre">{fallbackText ? fallbackText.replace(/\*/g, "") : "Bygger upp track record — varje skickat larm poängsätts mot prisrörelsen 1h senare."}</p>
      </div>
    );
  }
  const hits = scored.filter((a) => a.correct).length;
  const overall = Math.round((hits / scored.length) * 100);
  const recent = [...scored].sort((a, b) => (a.ts || 0) - (b.ts || 0)).slice(-40);

  return (
    <div className="card">
      <h3>Träffsäkerhet ({scored.length} poängsatta larm, 14d)</h3>
      <div style={{ marginBottom: 8 }}>
        <span className={`pill ${overall >= 55 ? "bull" : overall >= 45 ? "warn" : "bear"}`}>
          {overall}% rätt totalt
        </span>
      </div>
      <div className="dots">
        {recent.map((a, i) => (
          <span key={i} className={`sq ${a.correct ? "hit" : "miss"}`}
            title={`konv ${a.conv} · ${a.dir} · ${(a.ret * 100).toFixed(2)}%`} />
        ))}
      </div>
      {BANDS.map(([lo, hi, label]) => {
        const b = scored.filter((a) => (a.conv ?? 0) >= lo && (a.conv ?? 0) <= hi);
        if (b.length === 0) return null;
        const p = Math.round((b.filter((a) => a.correct).length / b.length) * 100);
        return (
          <div key={label} className="bandrow">
            <span className="sub" style={{ width: 56 }}>konv {label}</span>
            <div className="bar"><div className={`fill ${p >= 55 ? "g" : p >= 45 ? "y" : "r"}`}
              style={{ width: `${p}%` }} /></div>
            <span className="sub" style={{ width: 76, textAlign: "right" }}>{p}% · n={b.length}</span>
          </div>
        );
      })}
      <div className="sub" style={{ marginTop: 6 }}>
        Riktningsträff mot prisrörelsen 1h efter larmet. Högre band bör vara grönare — annars är tröskeln fel satt.
      </div>
    </div>
  );
}
