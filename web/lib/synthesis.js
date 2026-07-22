// Synthesis / Edge engine — fuses LIVE technicals with the engine's
// FUNDAMENTAL + regime read into one explainable conclusion. Every output is
// traceable to its inputs; no black box and no fabricated prediction.
//
// Quantitative grounding comes from the backtest (oiltrader/backtest.py):
//   • with-trend long RSI-reclaims had positive edge (~+0.2..+0.4R, 44-56% hit)
//   • short-fades had negative edge (~-0.5R)
// so recommendations are long-biased and shorts are gated to a real downtrend
// with confirmation. The domain regime logic (playbook.py) adds the crude-
// specific asymmetry: a *physical* supply disruption TRENDS (buy dips); a
// geopolitical *risk premium* MEAN-REVERTS (fade spikes / wait).

const REGIME = {
  "supply-risk":    { behav: "trend",       sv: "Fysisk utbudsstörning – riktiga fat borta → trendar, köp dippar med trenden." },
  "war-premium":    { behav: "revert",      sv: "Krigspremie utan bekräftat bortfall → mattas oftast, jaga inte toppar." },
  "premium-unwind": { behav: "revert_down", sv: "De-eskalering → premien släpper, studsar säljs." },
  "inventory":      { behav: "event",       sv: "Lagerdrivet → reagera på siffran, annars mean-reversion vid nivåer." },
  "opec":           { behav: "trend",       sv: "OPEC-styrt utbud → följ beskedets riktning." },
  "mixed":          { behav: "range",       sv: "Spretig bild → range/mean-reversion tills en tes vinner." },
};

const NEAR = 0.0035;

export function buildSynthesis(live, state) {
  if (!live || !live.levels || live.price == null) return null;
  const price = live.price;
  const lv = live.levels;
  const os = live.osDyn ?? 30, ob = live.obDyn ?? 70;

  // ---------------- technical read ----------------
  const res = lv.resistance?.[0]?.v ?? null;
  const sup = lv.support?.[0]?.v ?? null;
  const dRes = res ? (res - price) / price : null;
  const dSup = sup ? (price - sup) / price : null;
  const atLevel = (dRes != null && dRes >= 0 && dRes <= NEAR) ? "motstånd"
    : (dSup != null && dSup >= 0 && dSup <= NEAR) ? "stöd" : "mitt emellan";
  const rsiState = live.rsi >= ob ? "överköpt" : live.rsi <= os ? "översåld" : "neutral";
  const atrPct = live.atr && price ? (live.atr / price) * 100 : null;
  const volRegime = atrPct == null ? "okänd" : atrPct >= 0.9 ? "hög" : atrPct <= 0.35 ? "låg" : "normal";

  const mtf = state?.trend || {};
  const slowTrend = mtf["1h"] || mtf["4h"] || live.trend;   // up|down|sideways|flat
  const fastTrend = mtf["5m"] || live.trend;

  // Mean-reversion technical lean: the setup side the chart is offering.
  let techLean = "neutral";
  const techWhy = [];
  if (atLevel === "stöd" && rsiState !== "överköpt") {
    techLean = "long"; techWhy.push(rsiState === "översåld" ? "översåld vid stöd" : "vid stöd");
  } else if (atLevel === "motstånd" && rsiState !== "översåld") {
    techLean = "short"; techWhy.push(rsiState === "överköpt" ? "överköpt vid motstånd" : "vid motstånd");
  } else if (rsiState === "översåld") { techLean = "long"; techWhy.push("översåld"); }
  else if (rsiState === "överköpt") { techLean = "short"; techWhy.push("överköpt"); }

  const withSlow =
    (techLean === "long" && slowTrend === "up") ||
    (techLean === "short" && slowTrend === "down");
  const rejection = techLean === "long" ? (live.closePos >= 0.6)
    : techLean === "short" ? (live.closePos <= 0.4) : false;

  // ---------------- fundamental read ----------------
  const regime = state?.regime || "mixed";
  const rm = REGIME[regime] || REGIME.mixed;
  const fundBias = state?.bias === "bullish" ? "long"
    : state?.bias === "bearish" ? "short" : "neutral";
  const corr = state?.supply_corroboration || 0;

  const nowSec = Date.now() / 1000;
  const evs = state?.events || [];
  const scoreOf = (e) => (Number(e.rel) || 0) * Math.max(Number(e.sub) || 0, 0.15);
  const fresh = evs.filter((e) => e.ts && e.ts > nowSec - 45 * 60);
  const freshTop = [...fresh].sort((a, b) => scoreOf(b) - scoreOf(a))[0] || null;
  const driver = freshTop ? "nyhetsdriven" : (live.trend !== "flat" ? "flödesdriven" : "teknisk");
  const topDrivers = [...evs].sort((a, b) => scoreOf(b) - scoreOf(a)).slice(0, 2);

  // ---------------- fuse: regime-aware edge side ----------------
  // The insight: regime overrides the naive "bullish news → buy" reflex.
  let side = "wait";          // long | short | wait
  let label = "STÅ UTANFÖR";
  let tone = "neutral";       // bull | bear | warn | neutral
  const reasons = [];
  const conflicts = [];       // genuine technical-vs-fundamental opposition
  const notes = [];           // routine gating / no-setup info (not a conflict)

  const freshOpposes = freshTop && (
    (techLean === "long" && freshTop.dir === "bearish") ||
    (techLean === "short" && freshTop.dir === "bullish"));

  if (techLean === "long") {
    // Long reclaim at support — the backtested edge side.
    if (rm.behav === "revert_down" || fundBias === "short") {
      side = "wait"; label = "VÄNTA (mottrend-long)";
      conflicts.push("fundamenta/regim pekar ned – long vore mottrend");
    } else if (rm.behav === "trend" && (slowTrend === "up" || fundBias === "long")) {
      side = "long"; label = "KÖP-DIPP (med trend)"; tone = "bull";
      reasons.push("trendregim + dip mot stöd", ...techWhy);
    } else {
      side = "long"; label = "REVERSION LÅNG"; tone = "bull";
      reasons.push("mean-reversion vid stöd", ...techWhy);
    }
    if (side === "long" && !rejection) {
      side = "wait"; label = "VÄNTA PÅ RECLAIM"; tone = "neutral";
      notes.push("ingen avvisningsstake än – vänta på reclaim genom RSI-linjen");
    }
  } else if (techLean === "short") {
    // Short-fades backtested negative → only in a genuine downtrend + rejection.
    if (slowTrend === "down" && rejection &&
        (rm.behav === "revert_down" || rm.behav === "revert" || fundBias === "short")) {
      side = "short"; label = "FADE (försiktig, liten)"; tone = "bear";
      reasons.push("nedtrend + avvisning vid motstånd", ...techWhy);
    } else {
      side = "wait"; label = "AVSTÅ SHORT";
      notes.push("short-fade har negativ historisk edge – kräver klar nedtrend + avvisning");
    }
  } else {
    side = "wait"; label = "INGEN SETUP";
    notes.push("pris mitt emellan nivåer, RSI neutral – inget läge");
  }

  if (freshOpposes && side !== "wait") {
    conflicts.push(`färsk ${freshTop.dir === "bullish" ? "hausse" : "baisse"}-rubrik emot – momentum, fadea inte`);
    tone = "warn";
    if (side === "short") { side = "wait"; label = "VÄNTA (nyhet emot)"; }
  }
  if (conflicts.length && side !== "wait") tone = "warn";

  // ---------------- conviction 0-100 (transparent) ----------------
  let conv = 35;
  const cf = [];
  if (side !== "wait") {
    if (withSlow) { conv += 20; cf.push("+ med långsam trend"); }
    if (rejection) { conv += 12; cf.push("+ avvisningsstake"); }
    if (atLevel !== "mitt emellan") { conv += 10; cf.push(`+ vid ${atLevel}`); }
    // fundamental agreement with the trade side
    if ((side === "long" && fundBias === "long") || (side === "short" && fundBias === "short")) {
      conv += 12; cf.push("+ fundamenta i linje");
    }
    if (corr >= 3 && rm.behav === "trend") { conv += 8; cf.push("+ korroborerat utbud"); }
    if (volRegime === "hög") { conv -= 8; cf.push("− hög volatilitet (bredare stopp)"); }
  }
  if (conflicts.length) { conv -= 18 * Math.min(conflicts.length, 2); }
  conv = Math.max(5, Math.min(100, conv));

  // ---------------- honest edge note (backtest-grounded) ----------------
  let edge;
  if (side === "long") {
    edge = withSlow
      ? "Backtest: med-trend long-reclaim ~+0.3–0.7R, 45–56 % träff (litet urval, ~2 mån)."
      : "Backtest: long-reclaim positiv men svagare mot-trend (~+0.1R). Snålt mål, mindre storlek.";
  } else if (side === "short") {
    edge = "Backtest: short-fade negativ edge (~−0.5R) generellt – detta är undantaget (klar nedtrend). Liten storlek, tajt stopp.";
  } else {
    edge = "Ingen edge i uppmätt data just nu – vänta på ett läge där teknik och fundamenta pekar åt samma håll.";
  }

  // ---------------- scenarios (levels + regime basis) ----------------
  const scenarios = [];
  if (res) scenarios.push({
    trigger: `Över ${res.toFixed(2)}`,
    then: rm.behav === "trend"
      ? "utbrott kan trenda – köp återtest, jaga inte."
      : "in i motstånd i revert-regim – bevaka avvisning för fade/vänta.",
  });
  if (sup) scenarios.push({
    trigger: `Under ${sup.toFixed(2)}`,
    then: fundBias === "long" && rm.behav !== "revert_down"
      ? "dipp mot stöd – leta reclaim för köp (huvudscenario)."
      : "brott ned kan accelerera – fånga inte fallande kniv utan reclaim.",
  });

  const invalidation = side === "long" && sup ? `Under ${sup.toFixed(2)} utan reclaim → long-tesen faller.`
    : side === "short" && res ? `Över ${res.toFixed(2)} → fade-tesen faller.`
    : "Ett rent brott genom närmaste nivå ogiltigförklarar väntläget.";

  // ---------------- narrative (2-3 sentences) ----------------
  const drvTxt = driver === "nyhetsdriven" && freshTop
    ? `Rörelsen drivs av färsk ${freshTop.dir === "bullish" ? "hausse" : freshTop.dir === "bearish" ? "baisse" : "neutral"}-rubrik ("${(freshTop.title||"").slice(0,60)}…").`
    : driver === "flödesdriven"
      ? "Rörelsen är flödesdriven – ingen färsk rubrik bakom den."
      : "Lugnt läge – tekniskt styrt kring nivåerna.";
  const alignTxt = conflicts.length ? `Men ${conflicts[0]}.`
    : side !== "wait" ? "Teknik och fundamenta drar åt samma håll."
    : notes.length ? notes[0].charAt(0).toUpperCase() + notes[0].slice(1) + "."
    : "Inget entydigt läge.";
  const narrative = `${rm.sv} ${drvTxt} ${alignTxt}`;

  const alignment = conflicts.length ? "konflikt" : (side !== "wait" ? "samstämmig" : "neutral");

  // Event-study prior from the engine (null until its outcomes table matures).
  const a = state?.analog || null;
  const analog = a ? {
    ...a,
    text: `${a.n} liknande ${a.direction === "bullish" ? "hausse" : "baisse"}-${a.category} → ` +
      `${a.hit_pct}% i riktningen, median ${a.median_pct >= 0 ? "+" : ""}${a.median_pct}% (${a.horizon_h}h)`,
  } : null;

  return {
    analog,
    side, label, tone, conviction: conv,
    alignment,
    narrative,
    edge,
    reasons, conflicts, notes, convFactors: cf,
    scenarios, invalidation,
    driver,
    factors: [
      { k: "Teknik", v: `${slowTrend === "up" ? "trend upp" : slowTrend === "down" ? "trend ned" : "range"} · RSI ${Math.round(live.rsi)} (${rsiState}) · ${atLevel}`, tone: techLean === "long" ? "bull" : techLean === "short" ? "bear" : "neutral" },
      { k: "Fundamenta", v: `${state?.bias === "bullish" ? "hausse" : state?.bias === "bearish" ? "baisse" : "neutral"}${corr ? ` · korrob. ${corr}` : ""}`, tone: fundBias === "long" ? "bull" : fundBias === "short" ? "bear" : "neutral" },
      { k: "Regim", v: regime, tone: "neutral" },
      { k: "Drivkraft", v: driver, tone: "neutral" },
      { k: "Volatilitet", v: `${volRegime}${atrPct != null ? ` (ATR ${atrPct.toFixed(2)}%)` : ""}`, tone: volRegime === "hög" ? "warn" : "neutral" },
    ],
    topDrivers,
  };
}
