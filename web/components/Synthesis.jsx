"use client";
// The centerpiece: one fused, explainable read combining live technicals with
// the engine's fundamental/regime picture, plus a backtest-grounded edge note.
export default function Synthesis({ syn }) {
  if (!syn) {
    return (
      <section className="ol-card ol-syn">
        <div className="ol-cardhead"><span className="ol-cardtitle">Syntes &amp; Edge</span></div>
        <div className="ol-empty">Väntar på live-data för att väva ihop teknik och fundamenta…</div>
      </section>
    );
  }
  const toneCls = syn.tone || "neutral";
  return (
    <section className={`ol-card ol-syn tone-${toneCls}`}>
      <div className="ol-cardhead">
        <span className="ol-cardtitle">Syntes &amp; Edge</span>
        <span className={`ol-syn-align ${syn.alignment}`}>{syn.alignment}</span>
      </div>

      <div className="ol-syn-top">
        <div className="ol-syn-verdict">
          <span className={`ol-syn-side ${toneCls}`}>{syn.label}</span>
          <span className="ol-syn-narr">{syn.narrative}</span>
        </div>
        <div className="ol-syn-conv">
          <div className="ol-syn-conv-num">{syn.conviction}</div>
          <div className="ol-syn-conv-lbl">konviktion</div>
          <div className="ol-syn-conv-bar"><div className={`fill ${toneCls}`} style={{ width: `${syn.conviction}%` }} /></div>
        </div>
      </div>

      <div className="ol-syn-factors">
        {syn.factors.map((f, i) => (
          <div className="ol-syn-factor" key={i}>
            <span className="k">{f.k}</span>
            <span className={`v tone-${f.tone}`}>{f.v}</span>
          </div>
        ))}
      </div>

      <div className="ol-syn-edge">🎯 {syn.edge}</div>

      {syn.analog && (
        <div className="ol-syn-analog">📊 Historik: {syn.analog.text}</div>
      )}

      {syn.conflicts?.length > 0 && (
        <div className="ol-syn-conflicts">
          {syn.conflicts.map((c, i) => <div key={i}>⚠️ {c}</div>)}
        </div>
      )}
      {syn.notes?.length > 0 && (
        <div className="ol-syn-notes">
          {syn.notes.map((n, i) => <div key={i}>ℹ️ {n}</div>)}
        </div>
      )}

      {syn.scenarios?.length > 0 && (
        <div className="ol-syn-scen">
          {syn.scenarios.map((s, i) => (
            <div className="ol-syn-scen-row" key={i}>
              <span className="trig">{s.trigger}</span>
              <span className="then">{s.then}</span>
            </div>
          ))}
          <div className="ol-syn-inval">✗ {syn.invalidation}</div>
        </div>
      )}

      <div className="ol-syn-foot">
        Regelbaserad syntes · edge från backtest &amp; regimlogik, ej garanti · beslutsstöd, ej rådgivning.
      </div>
    </section>
  );
}
