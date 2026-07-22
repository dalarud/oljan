"use client";
// Visual track record: each scored alert as a dot (hit/miss) plus precision
// per conviction band — makes the engine's self-evaluation glanceable.

const BANDS = [
  [0, 50, "Låg konviktion (<50)"],
  [50, 75, "Medel (50–75)"],
  [75, 101, "Hög (>75)"],
];

export default function ScoreViz({ alerts, fallbackText }) {
  const all = alerts || [];
  const scored = all.filter((a) => a.correct != null);

  if (scored.length === 0) {
    return (
      <section className="ol-card">
        <div className="ol-cardhead">
          <span className="ol-cardtitle">Träffsäkerhet</span>
          <span className="ol-cardsub">Senaste 14 dagarna</span>
        </div>
        <div className="ol-track">
          <p className="ol-scorecard">
            {fallbackText ? fallbackText.replace(/\*/g, "")
              : "Bygger upp track record — varje skickat larm poängsätts mot prisrörelsen 1h senare."}
          </p>
        </div>
      </section>
    );
  }

  const hits = scored.filter((a) => a.correct === 1).length;
  const rate = Math.round((hits / scored.length) * 100);
  const dots = [...all].sort((a, b) => (b.ts || 0) - (a.ts || 0)).slice(0, 24);

  const bands = BANDS.map(([lo, hi, label]) => {
    const b = scored.filter((a) => (a.conv ?? 0) >= lo && (a.conv ?? 0) < hi);
    const bh = b.filter((a) => a.correct === 1).length;
    return { label, rate: b.length ? `${Math.round((bh / b.length) * 100)}%` : "—" };
  });

  return (
    <section className="ol-card">
      <div className="ol-cardhead">
        <span className="ol-cardtitle">Träffsäkerhet</span>
        <span className="ol-cardsub">Senaste 14 dagarna</span>
      </div>
      <div className="ol-track">
        <div className="ol-track-top">
          <span className="ol-hitrate">{rate}%</span>
          <span className="ol-hitcount">{hits} av {scored.length} poängsatta larm</span>
        </div>
        <div className="ol-dots">
          {dots.map((a, i) => (
            <span key={i}
              className={`ol-dot ${a.correct == null ? "pending" : a.correct ? "hit" : "miss"}`}
              title={`konv ${a.conv} · ${a.dir} · ${((a.ret ?? 0) * 100).toFixed(2)}%`} />
          ))}
        </div>
        <div className="ol-bands">
          {bands.map((b) => (
            <div className="ol-band" key={b.label}>
              <span className="ol-band-label">{b.label}</span>
              <span className="ol-band-rate">{b.rate}</span>
            </div>
          ))}
        </div>
        {fallbackText && <p className="ol-scorecard">{fallbackText.replace(/\*/g, "")}</p>}
      </div>
    </section>
  );
}
