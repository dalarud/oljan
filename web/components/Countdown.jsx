"use client";
// Session clock + countdown to the next market catalysts. Times are defined in
// their real scheduling timezones (New York / London) so DST never breaks them.
import { useEffect, useState } from "react";

// [weekday (0=Sun..6=Sat) or null for Mon-Fri, tz, hh, mm, label, hot]
const CATALYSTS = [
  [null, "Europe/London", 8, 0, "Europaöppning", false],
  [null, "America/New_York", 9, 30, "US-öppning", true],
  [2, "America/New_York", 16, 30, "API råoljelager", true],
  [3, "America/New_York", 10, 30, "EIA veckolager", true],
  [5, "America/New_York", 13, 0, "Baker Hughes riggar", false],
];

function zonedUtc(y, m, d, hh, mm, tz) {
  let ts = Date.UTC(y, m, d, hh, mm);
  for (let i = 0; i < 3; i++) {
    const p = Object.fromEntries(
      new Intl.DateTimeFormat("en-US", {
        timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", hour12: false,
      }).formatToParts(new Date(ts)).map((x) => [x.type, x.value])
    );
    const asIf = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute);
    ts += Date.UTC(y, m, d, hh, mm) - asIf;
  }
  return ts;
}

function nextOccurrence(weekday, tz, hh, mm) {
  const now = Date.now();
  for (let off = 0; off < 9; off++) {
    const d = new Date(now + off * 86400000);
    const parts = Object.fromEntries(
      new Intl.DateTimeFormat("en-US", {
        timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit", weekday: "short",
      }).formatToParts(d).map((x) => [x.type, x.value])
    );
    const wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].indexOf(parts.weekday);
    if (weekday == null ? (wd === 0 || wd === 6) : wd !== weekday) continue;
    const ts = zonedUtc(+parts.year, +parts.month - 1, +parts.day, hh, mm, tz);
    if (ts > now) return ts;
  }
  return null;
}

const fmt = (ms) => {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m ${Math.floor(s % 60)}s`;
};

function session() {
  const h = new Date().getUTCHours();
  if (h >= 13 && h < 21) return ["US-session", "tjock likviditet", "bull"];
  if (h >= 7 && h < 13) return ["Europa-session", "normal likviditet", "warn"];
  return ["Asien/natt", "tunn likviditet – spikar reverterar oförutsägbart", "neutral"];
}

export default function Countdown() {
  const [, tick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => tick((x) => x + 1), 1000);
    return () => clearInterval(t);
  }, []);

  const now = Date.now();
  const items = CATALYSTS
    .map(([wd, tz, hh, mm, label, hot]) => ({ label, hot, ts: nextOccurrence(wd, tz, hh, mm) }))
    .filter((x) => x.ts)
    .sort((a, b) => a.ts - b.ts)
    .slice(0, 3);
  const [sess, liq, cls] = session();

  return (
    <div className="card">
      <h3>Klocka & katalysatorer</h3>
      <div style={{ marginBottom: 8 }}>
        <span className={`pill ${cls}`}>{sess}</span>{" "}
        <span className="sub">{liq}</span>
      </div>
      {items.map((it, i) => (
        <div className="levelrow" key={i}>
          <span className={it.hot ? "lvl-piv" : ""}>{it.hot ? "⚡ " : ""}{it.label}</span>
          <span>
            {fmt(it.ts - now)}
            <span className="sub">
              {" · "}
              {new Date(it.ts).toLocaleTimeString("sv-SE", {
                timeZone: "Europe/Stockholm", hour: "2-digit", minute: "2-digit",
              })}
            </span>
          </span>
        </div>
      ))}
    </div>
  );
}
