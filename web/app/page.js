"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import TradingViewWidget from "../components/TradingViewWidget";
import RiskCalc from "../components/RiskCalc";
import ScoreViz from "../components/ScoreViz";
import Countdown from "../components/Countdown";
import { rsi, atr, computeLevels } from "../lib/indicators";

const LiveChart = dynamic(() => import("../components/LiveChart"), { ssr: false });

const RSI_OB = 70, RSI_OS = 30, NEAR = 0.0035, COOLDOWN_MS = 30 * 60 * 1000;

const dirClass = (d) => (d === "bullish" ? "bull" : d === "bearish" ? "bear" : "neutral");
const biasLabel = (d) => (d === "bullish" ? "HAUSSE" : d === "bearish" ? "BAISSE" : "NEUTRAL");
const fmtTime = (iso) => {
  if (!iso) return "–";
  try { return new Date(iso).toLocaleString("sv-SE", { timeZone: "Europe/Stockholm" }); }
  catch { return iso; }
};
const hhmm = (ts) => {
  if (!ts) return "";
  try {
    return new Date(ts * 1000).toLocaleTimeString("sv-SE",
      { timeZone: "Europe/Stockholm", hour: "2-digit", minute: "2-digit" });
  } catch { return ""; }
};

export default function Page() {
  const [s, setS] = useState(null);          // engine state.json
  const [candles, setCandles] = useState(null); // live futures candles
  const [liveMeta, setLiveMeta] = useState(null);
  const [err, setErr] = useState(null);
  const [alertsOn, setAlertsOn] = useState(false);
  const [banner, setBanner] = useState(null);
  const prevRsiRef = useRef(null);
  const cooldownRef = useRef({ long: 0, short: 0 });
  const audioRef = useRef(null);

  // ---- engine snapshot (intelligence layer), each minute -------------------
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const r = await fetch("/api/state", { cache: "no-store" });
        const data = await r.json();
        if (alive && data && !data.error) setS(data);
      } catch {}
    };
    load();
    const t = setInterval(load, 60000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  // ---- live real price (Vercel proxy), every 20s ---------------------------
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const r = await fetch("/api/price", { cache: "no-store" });
        const data = await r.json();
        if (!alive) return;
        if (data && data.candles) {
          setCandles(data.candles);
          setLiveMeta({ src: data.src, at: data.fetched_at });
          setErr(null);
        } else setErr(data?.error || "prisdata saknas");
      } catch (e) { if (alive) setErr(String(e)); }
    };
    load();
    const t = setInterval(load, 20000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const live = useMemo(() => {
    if (!candles || candles.length < 30) return null;
    const closes = candles.map((c) => c.c);
    return {
      price: closes[closes.length - 1],
      rsi: rsi(closes),
      atr: atr(candles),
      levels: computeLevels(candles),
      lastTs: candles[candles.length - 1].t,
    };
  }, [candles]);

  const liveAgeMin = live ? Math.max(0, (Date.now() / 1000 - live.lastTs) / 60) : null;
  const liveFresh = liveAgeMin != null && liveAgeMin < 20;

  // ---- calibration watch (#4) ---------------------------------------------
  const calib = useMemo(() => {
    if (!s || !live || s.price == null) return null;
    const isScaled = (s.price_source || "").includes("scaled");
    const drift = ((live.price - s.price) / live.price) * 100;
    const suggested = isScaled && s.scale_factor
      ? s.scale_factor * (live.price / s.price) : null;
    return { drift, isScaled, suggested };
  }, [s, live]);

  // ---- browser-side setup detection (#2): RSI reclaim at a level -----------
  useEffect(() => {
    if (!live || !liveFresh || live.rsi == null) return;
    const prev = prevRsiRef.current;
    prevRsiRef.current = live.rsi;
    if (prev == null) return;

    const lv = live.levels;
    const nearAny = (arr) =>
      (arr || []).find((x) => Math.abs(live.price - x.v) / live.price <= NEAR);
    const now = Date.now();
    let setup = null;
    if (prev <= RSI_OS && live.rsi > RSI_OS) {
      const at = nearAny(lv?.support);
      if (at && now - cooldownRef.current.long > COOLDOWN_MS) {
        cooldownRef.current.long = now;
        setup = { side: "KÖP", cls: "bull", at, target: lv?.pivot ?? lv?.resistance?.[0]?.v };
      }
    } else if (prev >= RSI_OB && live.rsi < RSI_OB) {
      const at = nearAny(lv?.resistance);
      if (at && now - cooldownRef.current.short > COOLDOWN_MS) {
        cooldownRef.current.short = now;
        setup = { side: "SÄLJ", cls: "bear", at, target: lv?.pivot ?? lv?.support?.[0]?.v };
      }
    }
    if (!setup) return;
    const msg = `SETUP ${setup.side} · RSI-reclaim ${Math.round(prev)}→${Math.round(live.rsi)} ` +
      `vid ${setup.at.label} ${setup.at.v} · pris ${live.price.toFixed(2)}` +
      (setup.target ? ` · mål ${Number(setup.target).toFixed(2)}` : "");
    setBanner({ ...setup, msg, ts: now });
    if (alertsOn) {
      beep(audioRef.current);
      try {
        if (typeof Notification !== "undefined" && Notification.permission === "granted")
          new Notification("Oljan – setup", { body: msg });
      } catch {}
    }
  }, [live, liveFresh, alertsOn]);

  const enableAlerts = async () => {
    try {
      audioRef.current = audioRef.current ||
        new (window.AudioContext || window.webkitAudioContext)();
      await audioRef.current.resume();
      if (typeof Notification !== "undefined" && Notification.permission === "default")
        await Notification.requestPermission();
      beep(audioRef.current);
      setAlertsOn(true);
    } catch { setAlertsOn(true); }
  };

  const sym = (s && s.tv_symbol) || "TVC:UKOIL";
  const displayPrice = live ? live.price : s?.price;
  const displayRsi = live?.rsi != null ? Math.round(live.rsi) : s?.rsi;

  return (
    <div className="wrap">
      <div className="topbar">
        <span className="brand">🛢️ Oljan</span>
        <div>
          <div className="price">{displayPrice != null ? Number(displayPrice).toFixed(2) : "–"}</div>
          <div className="sub">
            {live ? <>Brent-terminer live{liveFresh ? "" : <span className="stale"> · {Math.round(liveAgeMin)}m gammal</span>}</>
              : (s ? s.instrument : "laddar…")}
            {displayRsi != null ? ` · RSI ${displayRsi}` : ""}
            {live?.atr != null ? ` · ATR ${live.atr.toFixed(2)}` : ""}
          </div>
        </div>
        {s && <span className={`pill ${dirClass(s.bias)}`}>{biasLabel(s.bias)}</span>}
        {s?.regime && <span className="pill warn">regim: {s.regime}</span>}
        {s?.trend && Object.keys(s.trend).length > 0 && (
          <span className="pill neutral">
            MTF {Object.entries(s.trend).map(([k, v]) =>
              `${k} ${v === "up" ? "↑" : v === "down" ? "↓" : "→"}`).join(" · ")}
          </span>
        )}
        <span className="spacer" />
        {!alertsOn
          ? <button className="btn" onClick={enableAlerts}>🔔 Aktivera larm</button>
          : <span className="pill bull">🔔 larm på</span>}
        <span className="sub">motorn: {fmtTime(s && s.updated_at)}</span>
      </div>

      {banner && (
        <div className={`setupbanner ${banner.cls}`}>
          <strong>⚡ {banner.msg}</strong>
          <span className="sub"> · fade aldrig in i en färsk rubrik — kolla flödet nedan</span>
          <button className="btn ghost" onClick={() => setBanner(null)}>✕</button>
        </div>
      )}
      {err && !candles && (
        <div className="card err" style={{ marginBottom: 14 }}>
          Live-pris ej tillgängligt just nu ({err}) — visar motorns senaste data.
        </div>
      )}

      <div className="grid">
        <div className="chart"><TradingViewWidget symbol={sym} /></div>

        <div className="side">
          <Countdown />
          <div className="card">
            <h3>Nyckelnivåer (live, riktig data)</h3>
            {live?.levels ? (
              <>
                {live.levels.resistance.map((r, i) => (
                  <div className="levelrow" key={"r" + i}>
                    <span className="lvl-res">▲ {r.label}</span><span>{r.v.toFixed(2)}</span>
                  </div>
                ))}
                {live.levels.pivot != null && (
                  <div className="levelrow">
                    <span className="lvl-piv">◆ pivot/VWAP</span><span>{live.levels.pivot.toFixed(2)}</span>
                  </div>
                )}
                {live.levels.support.map((r, i) => (
                  <div className="levelrow" key={"s" + i}>
                    <span className="lvl-sup">▼ {r.label}</span><span>{r.v.toFixed(2)}</span>
                  </div>
                ))}
              </>
            ) : <div className="sub">Väntar på live-data…</div>}
            {calib && calib.isScaled && (
              <div className="sub" style={{ marginTop: 8 }}>
                Motorns bas avviker {calib.drift >= 0 ? "+" : ""}{calib.drift.toFixed(2)}%
                {Math.abs(calib.drift) > 0.4 && calib.suggested
                  ? <> → föreslagen ny faktor <code>{calib.suggested.toFixed(6)}</code></>
                  : " (inom tolerans)"}
              </div>
            )}
          </div>
        </div>

        <div className="card full">
          <h3>Analys-chart: nivåer + underrättelser på tidslinjen</h3>
          <LiveChart candles={candles} levels={live?.levels}
            events={s?.events} height={340} />
          <div className="sub" style={{ marginTop: 6 }}>
            Riktiga Brent-terminer (5m) · linjer = beräknade nivåer · pilar = underrättelser (grön hausse / röd baisse).
          </div>
        </div>

        <div className="card">
          <h3>Dagens plan (motorn)</h3>
          {s?.plan ? (
            <ul className="plan">{s.plan.map((l, i) => <li key={i}>{l.replace(/\*/g, "")}</li>)}</ul>
          ) : <div className="sub">–</div>}
        </div>

        <RiskCalc price={live?.price} atrVal={live?.atr} leverage={10}
          target={live?.levels?.resistance?.[0]?.v} />

        <div className="card full">
          <h3>Underrättelseflöde (senaste 24h)</h3>
          {s?.events?.length > 0 ? s.events.map((e, i) => (
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
          <p className="pre">{s?.pulse ? s.pulse.replace(/\*/g, "") : "–"}</p>
        </div>

        <ScoreViz alerts={s?.alerts} fallbackText={s?.scorecard} />
      </div>

      <div className="foot">
        Oljan · beslutsstöd, ej finansiell rådgivning · chart av TradingView ·
        live-pris = riktiga Brent-terminer via proxy · nivåer beräknade på riktig data ·
        underrättelser/plan från motorn.
      </div>
    </div>
  );
}

function beep(ctx) {
  if (!ctx) return;
  try {
    const t0 = ctx.currentTime;
    [0, 0.45].forEach((off) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.connect(g); g.connect(ctx.destination);
      o.frequency.value = 880;
      g.gain.setValueAtTime(0.12, t0 + off);
      g.gain.exponentialRampToValueAtTime(0.001, t0 + off + 0.3);
      o.start(t0 + off); o.stop(t0 + off + 0.32);
    });
  } catch {}
}
