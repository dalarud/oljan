"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import TradingViewWidget from "../components/TradingViewWidget";
import RiskCalc from "../components/RiskCalc";
import ScoreViz from "../components/ScoreViz";
import Countdown from "../components/Countdown";
import Synthesis from "../components/Synthesis";
import { rsi, atr, ema, computeLevels, rsiBands } from "../lib/indicators";
import { buildSynthesis } from "../lib/synthesis";

const LiveChart = dynamic(() => import("../components/LiveChart"), { ssr: false });

const NEAR = 0.0035, COOLDOWN_MS = 30 * 60 * 1000;
// Rejection-candle gates (close position within the trigger bar's range) and
// the short switch — mirrors the engine (setups.py). Shorts backtested to
// negative edge on this instrument, so they are OFF: an overbought reclaim
// surfaces as a caution, never as an actionable SÄLJ signal.
const REJECT_LONG = 0.60, REJECT_SHORT = 0.40, ALLOW_SHORTS = false;

const hhmm = (ts) => {
  if (!ts) return "";
  try {
    return new Date(ts * 1000).toLocaleTimeString("sv-SE",
      { timeZone: "Europe/Stockholm", hour: "2-digit", minute: "2-digit" });
  } catch { return ""; }
};
const timeAgo = (ts) => {
  if (!ts) return "";
  const m = Math.max(0, Math.round(Date.now() / 1000 / 60 - ts / 60));
  if (m < 1) return "nyss";
  if (m < 60) return `${m} min sedan`;
  const h = Math.floor(m / 60);
  return `${h} tim sedan`;
};
const dirWord = (d) => (d === "bullish" ? "hausse" : d === "bearish" ? "baisse" : "neutral");
// Relevance bucket from the engine's own ranking score (relevance × substance).
const relBucket = (rel, sub) => {
  const score = (Number(rel) || 0) * Math.max(Number(sub) || 0, 0.15);
  if (score >= 2.0) return { label: "HÖG", cls: "hog", score };
  if (score >= 1.0) return { label: "MEDEL", cls: "medel", score };
  return { label: "LÅG", cls: "lag", score };
};

export default function Page() {
  const [s, setS] = useState(null);          // engine state.json
  const [candles, setCandles] = useState(null); // live futures candles
  const [liveMeta, setLiveMeta] = useState(null);
  const [err, setErr] = useState(null);
  const [alertsOn, setAlertsOn] = useState(false);
  const [banner, setBanner] = useState(null);
  const [newsSort, setNewsSort] = useState("rel"); // "rel" | "tid"
  const prevRsiRef = useRef(null);
  const cooldownRef = useRef({ long: 0, short: 0 });
  const audioRef = useRef(null);
  // Manual calibration to the user's own UKOIL screen (additive offset in
  // points). The free BZ=F feed diverges from UKOIL (delay, contract rolls),
  // so the only reliable anchor is the price the user actually sees.
  const [calOffset, setCalOffset] = useState(null);
  const [calInput, setCalInput] = useState("");
  useEffect(() => {
    try {
      const v = localStorage.getItem("oljan_cal_offset");
      if (v != null && v !== "") setCalOffset(parseFloat(v));
    } catch {}
  }, []);

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
          setLiveMeta({ src: data.src, at: data.fetched_at, realtime: !!data.realtime });
          setErr(null);
        } else setErr(data?.error || "prisdata saknas");
      } catch (e) { if (alive) setErr(String(e)); }
    };
    load();
    const t = setInterval(load, 20000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  // The free BZ=F feed carries contract-roll / bad-tick discontinuities — a
  // sudden multi-point cliff that then holds (e.g. ~95 -> ~85). Splice those
  // gaps out so the series is continuous on the *latest* basis: walk back from
  // the newest bar and shift everything before each big jump by that jump, so
  // history lines up with "now". This keeps every candle (indicators/levels
  // need the count) instead of dropping a whole segment.
  const splicedCandles = useMemo(() => {
    if (!candles || candles.length < 10) return candles;
    const c = candles.map((x) => x.c);
    const diffs = [];
    for (let i = 1; i < c.length; i++) diffs.push(Math.abs(c[i] - c[i - 1]));
    const sorted = [...diffs].sort((a, b) => a - b);
    const med = sorted[Math.floor(sorted.length / 2)] || 0.1;
    const thr = Math.max(3.0, med * 10); // only egregious gaps count as breaks
    const shift = new Array(candles.length).fill(0);
    let acc = 0;
    for (let i = c.length - 1; i >= 1; i--) {
      if (Math.abs(c[i] - c[i - 1]) > thr) acc += c[i] - c[i - 1];
      shift[i - 1] = acc;
    }
    if (acc === 0) return candles;
    return candles.map((x, i) => shift[i] === 0 ? x : ({
      t: x.t, o: x.o + shift[i], h: x.h + shift[i], l: x.l + shift[i],
      c: x.c + shift[i], v: x.v,
    }));
  }, [candles]);

  const rawLastClose = splicedCandles?.length
    ? splicedCandles[splicedCandles.length - 1].c : null;

  // Additive offset onto the user's UKOIL basis. Manual calibration wins; the
  // (possibly stale) engine price is only a rough fallback until the user
  // calibrates. Additive because BZ=F↔UKOIL is a roughly constant spread.
  const realtime = !!liveMeta?.realtime;
  const effOffset = useMemo(() => {
    if (calOffset != null && isFinite(calOffset)) return calOffset;
    // A real-time feed (Oanda Brent) is already on the right basis — trust it.
    // Only the delayed Yahoo fallback needs the engine price as a rough anchor.
    if (realtime) return 0;
    if (s?.price != null && rawLastClose) return s.price - rawLastClose;
    return 0;
  }, [calOffset, realtime, s?.price, rawLastClose]);

  const scaledCandles = useMemo(() => {
    if (!splicedCandles || splicedCandles.length === 0) return splicedCandles;
    if (!effOffset) return splicedCandles;
    return splicedCandles.map((c) => ({
      t: c.t, o: c.o + effOffset, h: c.h + effOffset, l: c.l + effOffset,
      c: c.c + effOffset, v: c.v,
    }));
  }, [splicedCandles, effOffset]);

  const calibrated = calOffset != null && isFinite(calOffset);
  const applyCalibration = () => {
    const v = parseFloat(calInput);
    if (!isFinite(v) || !rawLastClose) return;
    const off = v - rawLastClose;
    setCalOffset(off);
    try { localStorage.setItem("oljan_cal_offset", String(off)); } catch {}
    setCalInput("");
  };

  const live = useMemo(() => {
    if (!scaledCandles || scaledCandles.length < 30) return null;
    const closes = scaledCandles.map((c) => c.c);
    const eFast = ema(closes, 12), eSlow = ema(closes, 26);
    const trend = eFast != null && eSlow != null
      ? (eFast > eSlow * 1.0003 ? "up" : eFast < eSlow * 0.9997 ? "down" : "flat")
      : "flat";
    const bands = rsiBands(closes);
    const lc = scaledCandles[scaledCandles.length - 1];
    const rng = lc.h - lc.l;
    const closePos = rng > 0 ? (lc.c - lc.l) / rng : 0.5;
    return {
      price: closes[closes.length - 1],
      rsi: rsi(closes),
      atr: atr(scaledCandles),
      trend,
      levels: computeLevels(scaledCandles),
      lastTs: scaledCandles[scaledCandles.length - 1].t,
      osDyn: bands.os,
      obDyn: bands.ob,
      closePos,
    };
  }, [scaledCandles]);

  const liveAgeMin = live ? Math.max(0, (Date.now() / 1000 - live.lastTs) / 60) : null;
  const liveFresh = liveAgeMin != null && liveAgeMin < 20;

  // Fused technical + fundamental read (the "edge" synthesis).
  const syn = useMemo(() => buildSynthesis(live, s), [live, s]);


  // ---- browser-side setup detection (#2): RSI reclaim at a level -----------
  useEffect(() => {
    if (!live || !liveFresh || live.rsi == null) return;
    const prev = prevRsiRef.current;
    prevRsiRef.current = live.rsi;
    if (prev == null) return;

    const lv = live.levels;
    const os = live.osDyn ?? 30, ob = live.obDyn ?? 70;
    const nearAny = (arr) =>
      (arr || []).find((x) => Math.abs(live.price - x.v) / live.price <= NEAR);
    const now = Date.now();
    let setup = null;
    // Long reclaim: RSI back above the ADAPTIVE oversold line, at support, with
    // a rejection candle (close snapping off the low). This is the edge side.
    if (prev <= os && live.rsi > os) {
      const at = nearAny(lv?.support);
      if (at && live.closePos >= REJECT_LONG &&
          now - cooldownRef.current.long > COOLDOWN_MS) {
        cooldownRef.current.long = now;
        setup = { side: "KÖP", cls: "bull", at, target: lv?.pivot ?? lv?.resistance?.[0]?.v };
      }
    } else if (prev >= ob && live.rsi < ob) {
      // Overbought reclaim -> short. Negative edge on this instrument, so it is
      // never an actionable signal: surface a caution and stop.
      const at = nearAny(lv?.resistance);
      if (at && now - cooldownRef.current.short > COOLDOWN_MS) {
        cooldownRef.current.short = now;
        const downOk = ALLOW_SHORTS && live.trend === "down" &&
          live.closePos <= REJECT_SHORT;
        const cmsg = downOk
          ? `⚠️ SETUP SÄLJ (försiktig) · RSI-reclaim ${Math.round(prev)}→${Math.round(live.rsi)} ` +
            `vid ${at.label} ${at.v} · short-reversion har svag edge här – liten storlek, tajt stopp`
          : `ℹ️ Överköpt-reclaim vid ${at.label} ${at.v}, men short-fade har negativ historisk edge på detta instrument – avstå eller vänta på riktig nedtrend + avvisning`;
        setBanner({ side: "SÄLJ", cls: "bear", msg: cmsg, ts: now, warn: true });
      }
      return;
    }
    if (!setup) return;
    // A long in a downtrend is counter-trend: warn, don't cheer.
    const counterTrend = live.trend === "down";
    // Fresh-headline guard: recent opposite-direction intel = momentum, not reversion.
    const winSec = 30 * 60;
    const freshOpp = (s?.events || []).filter((e) =>
      e.ts && e.ts > now / 1000 - winSec && e.dir === "bearish").length;
    // Confluence quality, same shape as the engine.
    let quality = 40;
    quality += counterTrend ? 0 : 25;
    quality += live.closePos >= 0.75 ? 20 : 10;
    quality += freshOpp > 0 ? -25 : 10;
    quality = Math.max(0, Math.min(100, quality));
    let prefix = "";
    if (counterTrend)
      prefix = `⚠️ MOTTREND (trend ${live.trend}${s?.regime ? `, regim ${s.regime}` : ""}) – litet/tajt eller avstå · `;
    if (freshOpp > 0)
      prefix += `⚠️ ${freshOpp} färsk motstående rubrik – momentum, fadea inte · `;
    const msg = `${prefix}SETUP ${setup.side} · RSI-reclaim ${Math.round(prev)}→${Math.round(live.rsi)} ` +
      `(OS-linje ${os.toFixed(0)}) vid ${setup.at.label} ${setup.at.v} · pris ${live.price.toFixed(2)} · kvalitet ${quality}/100` +
      (setup.target ? ` · mål ${Number(setup.target).toFixed(2)}` : "");
    setBanner({ ...setup, msg, ts: now, warn: counterTrend || freshOpp > 0 });
    if (alertsOn) {
      beep(audioRef.current);
      try {
        if (typeof Notification !== "undefined" && Notification.permission === "granted")
          new Notification("Oljan – setup", { body: msg });
      } catch {}
    }
  }, [live, liveFresh, alertsOn, s]);

  // ---- browser momentum alert: sustained move, headline or not -------------
  const momCooldownRef = useRef(0);
  useEffect(() => {
    if (!scaledCandles || scaledCandles.length < 12 || !liveFresh || !live) return;
    const nowSec = scaledCandles[scaledCandles.length - 1].t;
    const winMin = 45;
    const past = scaledCandles.filter((c) => c.t <= nowSec - winMin * 60);
    const base = past.length ? past[past.length - 1].c : scaledCandles[0].c;
    if (!base) return;
    const pct = ((live.price - base) / base) * 100;
    const thr = Math.max(0.5, live.atr ? (1.5 * live.atr / live.price) * 100 : 0);
    if (Math.abs(pct) < thr) return;
    if (Date.now() - momCooldownRef.current < 45 * 60 * 1000) return;
    momCooldownRef.current = Date.now();
    const up = pct > 0;
    const fresh = (s?.events || []).filter((e) => e.ts && e.ts > nowSec - winMin * 60).length;
    const msg = `MOMENTUM ${up ? "UPP" : "NED"} ${pct >= 0 ? "+" : ""}${pct.toFixed(1)}% på ${winMin}m · ` +
      `pris ${live.price.toFixed(2)} · ` +
      (fresh ? `${fresh} färska rubriker` : "ingen ny rubrik – flödesdrivet") +
      ` · fadea INTE utan reclaim`;
    setBanner({ side: up ? "MOMENTUM ↑" : "MOMENTUM ↓", cls: up ? "bull" : "bear", msg, warn: true });
    if (alertsOn) {
      beep(audioRef.current);
      try {
        if (typeof Notification !== "undefined" && Notification.permission === "granted")
          new Notification("Oljan – momentum", { body: msg });
      } catch {}
    }
  }, [scaledCandles, live, liveFresh, alertsOn, s]);

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
  const displayAtr = live?.atr != null ? live.atr.toFixed(2) : "—";

  const trendGlyph = live ? (live.trend === "up" ? "↑" : live.trend === "down" ? "↓" : "→") : "→";
  const trendColor = live?.trend === "up" ? "var(--green)"
    : live?.trend === "down" ? "var(--red)" : "var(--gray)";
  const biasTxt = s?.bias === "bullish" ? "Hausse" : s?.bias === "bearish" ? "Baisse" : "Neutral";
  const biasColor = s?.bias === "bullish" ? "var(--green)"
    : s?.bias === "bearish" ? "var(--red)" : "var(--gray)";
  const mtfTxt = s?.trend && Object.keys(s.trend).length
    ? Object.entries(s.trend).map(([k, v]) =>
        `${k} ${v === "up" ? "↑" : v === "down" ? "↓" : "→"}`).join(" · ")
    : null;

  const bannerCls = banner ? (banner.warn ? "warn" : banner.cls === "bear" ? "bear" : "bull") : "";
  const qMatch = banner?.msg?.match(/kvalitet\s*(\d+)\s*\/\s*100/i);
  const qualityScore = qMatch ? qMatch[1] : null;
  const updatedAgo = s?.updated_at
    ? `uppdaterad för ${Math.max(1, Math.round((Date.now() - new Date(s.updated_at).getTime()) / 60000))} min sedan`
    : "väntar på motorn…";

  return (
    <div className="ol-root">
      {/* 1. TOPPRAD */}
      <header className="ol-header">
        <div className="ol-brand">
          <span className="ol-mark" />
          <span className="ol-wordmark">Oljan</span>
          <span className="ol-livedot" />
        </div>

        <div className="ol-pricewrap">
          <div className="ol-priceline">
            <span className={`ol-price${!realtime && !calibrated ? " est" : ""}`}>
              {displayPrice != null ? Number(displayPrice).toFixed(2) : "–"}
            </span>
            <span className="ol-trend-glyph" style={{ color: trendColor }}>{trendGlyph}</span>
            {!realtime && !calibrated && <span className="ol-est-tag">uppskattat</span>}
          </div>
          <div className="ol-pricesub">
            <span>{live ? (realtime ? "Brent realtid (Oanda)" : "Brent BZ=F · fördröjd") : (s ? s.instrument : "laddar…")}</span>
            {live && !realtime && !liveFresh && <span className="ol-stale">· {Math.round(liveAgeMin)} min gammal</span>}
            {!realtime && live && (
              <span className="ol-cal-inline">
                <span className="sep">·</span>
                <input className="ol-cal-mini" inputMode="decimal"
                  placeholder="ditt UKOIL"
                  value={calInput} onChange={(e) => setCalInput(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") applyCalibration(); }} />
                <button className="ol-cal-mini-btn" onClick={applyCalibration}>synka</button>
                {calibrated && <span className="ol-cal-ok">✓ {effOffset >= 0 ? "+" : ""}{effOffset.toFixed(2)}</span>}
              </span>
            )}
          </div>
        </div>

        <div className="ol-metrics">
          <div className="ol-metric"><span className="ol-metric-label">RSI</span><span className="ol-metric-val">{displayRsi ?? "—"}</span></div>
          <div className="ol-metric"><span className="ol-metric-label">ATR</span><span className="ol-metric-val">{displayAtr}</span></div>
          <div className="ol-metric"><span className="ol-metric-label">Trend</span><span className="ol-metric-val" style={{ color: trendColor }}>{trendGlyph}</span></div>
        </div>

        <div className="ol-chips">
          {s && <span className="ol-chip" style={{ color: biasColor }}>
            <span className="ol-chip-dot" style={{ background: biasColor }} />{biasTxt}</span>}
          {s?.regime && <span className="ol-chip">
            <span className="ol-chip-dot" style={{ background: "var(--accent)" }} />{s.regime}</span>}
          {mtfTxt && <span className="ol-chip">
            <span className="ol-chip-dot" style={{ background: "var(--text-faint)" }} />{mtfTxt}</span>}
        </div>

        <div className="ol-alarm-wrap">
          <button className={`ol-alarm-btn${alertsOn ? " on" : ""}`}
            onClick={alertsOn ? () => setAlertsOn(false) : enableAlerts}>
            {alertsOn ? "🔔 Larm på" : "🔔 Aktivera larm"}
          </button>
          <span className="ol-motor">Motor {updatedAgo}</span>
        </div>
      </header>

      {/* 2. SIGNALBANNER */}
      {banner && (
        <div className={`ol-banner ${bannerCls}`}>
          <div className="ol-banner-left">
            <span className="ol-banner-badge">{banner.side}</span>
            <span className="ol-banner-msg">{banner.msg}</span>
            {qualityScore && <span className="ol-quality">Kvalitet {qualityScore}/100</span>}
          </div>
          <div className="ol-banner-right">
            {banner.warn && <span className="ol-banner-warntext">Fadea inte utan reclaim</span>}
            <button className="ol-banner-close" onClick={() => setBanner(null)} aria-label="Stäng">✕</button>
          </div>
        </div>
      )}
      {err && !candles && (
        <div className="ol-err">Live-pris ej tillgängligt just nu ({err}) — visar motorns senaste data.</div>
      )}

      {/* MAIN */}
      <main className="ol-main">
        <div className="ol-maincol">

          {/* 0. SYNTES & EDGE — fused technical + fundamental read */}
          <Synthesis syn={syn} />

          {/* 3. TRADINGVIEW */}
          <section className="ol-card">
            <div className="ol-cardhead">
              <span className="ol-cardtitle">UKOIL · TradingView</span>
              <span className="ol-cardsub">{sym}</span>
            </div>
            <div className="ol-tvbox"><TradingViewWidget symbol={sym} /></div>
          </section>

          {/* 6. ANALYS-CHART */}
          <section className="ol-card">
            <div className="ol-cardhead">
              <span className="ol-cardtitle">Analys · 5m-terminer med nivåer &amp; underrättelser</span>
              <span className="ol-cardsub">
                {realtime ? "Oanda Brent · realtid · svensk tid" : "BZ=F · svensk tid"}
                {liveAgeMin != null && (
                  <span className={!realtime && liveAgeMin > 20 ? "ol-stale" : ""}> · {Math.round(liveAgeMin)} min</span>
                )}
              </span>
            </div>
            <div className="ol-analysisbox">
              <LiveChart candles={scaledCandles} levels={live?.levels} events={s?.events} height={300} />
            </div>
            {!realtime && liveAgeMin != null && liveAgeMin >= 8 && (
              <p className="ol-risk-hint" style={{ paddingTop: 0 }}>
                Yahoos gratis-feed är ~10–15 min fördröjd; TradingView-charten ovan är realtid. Aktivera Oanda-feed för realtid i panelen.
              </p>
            )}
          </section>

          {/* 7. DAGENS PLAN */}
          <section className="ol-card">
            <div className="ol-cardhead"><span className="ol-cardtitle">Dagens plan</span></div>
            {s?.plan?.length ? (
              <ul className="ol-plan">{s.plan.map((l, i) => <li key={i}>{l.replace(/\*/g, "")}</li>)}</ul>
            ) : <div className="ol-empty">–</div>}
          </section>

          {/* 8. RISKKALKYLATOR */}
          <RiskCalc price={live?.price} atrVal={live?.atr} leverage={10}
            target={live?.levels?.resistance?.[0]?.v} />

          {/* 9. UNDERRÄTTELSEFLÖDE */}
          <section className="ol-card">
            <div className="ol-cardhead">
              <span className="ol-cardtitle">Underrättelseflöde</span>
              <span className="ol-news-sort">
                <button className={newsSort === "rel" ? "on" : ""} onClick={() => setNewsSort("rel")}>Relevans</button>
                <button className={newsSort === "tid" ? "on" : ""} onClick={() => setNewsSort("tid")}>Tid</button>
              </span>
            </div>
            {s?.events?.length > 0 ? (
              <div className="ol-news">
                {[...s.events]
                  .map((e) => ({ e, b: relBucket(e.rel, e.sub) }))
                  .sort((a, b) => newsSort === "tid"
                    ? (b.e.ts || 0) - (a.e.ts || 0)
                    : b.b.score - a.b.score)
                  .map(({ e, b }, i) => (
                    <a className={`ol-news-row rel-${b.cls}`} key={i} href={e.url || "#"}
                      target={e.url ? "_blank" : undefined} rel="noopener noreferrer"
                      style={{ borderLeft: `3px solid ${dirColorVar(e.dir)}` }}>
                      <span className={`ol-news-rel ${b.cls}`}>{b.label}</span>
                      <span className="ol-news-body">
                        <span className="ol-news-title">{e.title}</span>
                        <span className="ol-news-meta">
                          <span style={{ color: dirColorVar(e.dir) }}>{dirWord(e.dir)}</span>
                          {" · "}{timeAgo(e.ts)}{" · "}{e.cat}
                        </span>
                      </span>
                    </a>
                  ))}
              </div>
            ) : <div className="ol-empty">Inga relevanta händelser i fönstret.</div>}
          </section>

          {/* 10. MARKNADSPULS */}
          <section className="ol-card">
            <div className="ol-cardhead"><span className="ol-cardtitle">Marknadspuls</span></div>
            <p className="ol-pulse">{s?.pulse ? s.pulse.replace(/\*/g, "") : "–"}</p>
          </section>

          {/* 11. TRÄFFSÄKERHET */}
          <ScoreViz alerts={s?.alerts} fallbackText={s?.scorecard} />
        </div>

        {/* SIDOPANEL */}
        <div className="ol-sidecol">
          <Countdown />

          {/* 5. NYCKELNIVÅER */}
          <section className="ol-card">
            <div className="ol-cardhead"><span className="ol-cardtitle">Nyckelnivåer</span></div>
            {live?.levels ? (
              <div className="ol-levels">
                {live.levels.resistance.map((r, i) => (
                  <div className="ol-level-row" key={"r" + i}>
                    <span className="ol-level-glyph res">▲</span>
                    <span className="ol-level-label">{r.label}</span>
                    <span className="ol-level-price">{r.v.toFixed(2)}</span>
                  </div>
                ))}
                {live.levels.pivot != null && (
                  <div className="ol-pivot-row">
                    <span className="ol-level-glyph piv">◆</span>
                    <span className="ol-level-label">Pivot / VWAP</span>
                    <span className="ol-level-price">{live.levels.pivot.toFixed(2)}</span>
                  </div>
                )}
                {live.levels.support.map((r, i) => (
                  <div className="ol-level-row" key={"s" + i}>
                    <span className="ol-level-glyph sup">▼</span>
                    <span className="ol-level-label">{r.label}</span>
                    <span className="ol-level-price">{r.v.toFixed(2)}</span>
                  </div>
                ))}
              </div>
            ) : <div className="ol-empty">Väntar på live-data…</div>}
            <div className="ol-calib">
              <div className="ol-cal-row">
                <span>UKOIL nu:</span>
                <input className="ol-cal-input" inputMode="decimal"
                  placeholder={live ? live.price.toFixed(2) : "94.50"}
                  value={calInput} onChange={(e) => setCalInput(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") applyCalibration(); }} />
                <button className="ol-cal-btn" onClick={applyCalibration}>Kalibrera</button>
              </div>
              <div style={{ marginTop: 6 }}>
                {calibrated
                  ? <>Kalibrerad mot din UKOIL (offset {effOffset >= 0 ? "+" : ""}{effOffset.toFixed(2)}). Justera om det glider.</>
                  : realtime
                    ? <>✅ Realtidsfeed aktiv (Oanda Brent). Finjustera mot din UKOIL vid behov.</>
                    : <>⚠️ Fördröjd/okalibrerad feed — visar {s?.price ? "motorns bas (kan släpa)" : "rå BZ=F"}. Aktivera Oanda-feed för realtid, eller skriv priset du ser och tryck Kalibrera.</>}
              </div>
            </div>
          </section>
        </div>
      </main>

      {/* 12. SIDFOT */}
      <footer className="ol-footer">
        <span>Beslutsstöd, inte finansiell rådgivning. Handel med hävstång innebär hög risk.</span>
        <span className="faint">Pris: {realtime ? "Oanda Brent realtid" : "BZ=F fördröjd"}{calibrated ? " · kalibrerad mot din UKOIL" : ""} · underrättelser/plan från motorn</span>
      </footer>
    </div>
  );
}

const dirColorVar = (d) =>
  d === "bullish" ? "var(--green)" : d === "bearish" ? "var(--red)" : "var(--gray)";

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
