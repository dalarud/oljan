"use client";
import React, { useEffect, useRef, memo } from "react";

function TradingViewWidget({ symbol = "TVC:UKOIL" }) {
  const container = useRef();

  useEffect(() => {
    if (!container.current) return;
    container.current.innerHTML =
      '<div class="tradingview-widget-container__widget" style="height:calc(100% - 32px);width:100%"></div>';
    const script = document.createElement("script");
    script.src =
      "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";
    script.type = "text/javascript";
    script.async = true;
    script.innerHTML = JSON.stringify({
      allow_symbol_change: true,
      calendar: false,
      details: false,
      hide_side_toolbar: true,
      hide_top_toolbar: false,
      hide_legend: false,
      hide_volume: false,
      hotlist: false,
      interval: "5",
      locale: "en",
      save_image: true,
      style: "1",
      symbol,
      theme: "dark",
      timezone: "Europe/Stockholm",
      backgroundColor: "#131519",
      gridColor: "rgba(242, 242, 242, 0.06)",
      watchlist: [],
      withdateranges: false,
      compareSymbols: [],
      studies: ["STD;RSI"],
      autosize: true,
    });
    container.current.appendChild(script);
  }, [symbol]);

  return (
    <div
      className="tradingview-widget-container"
      ref={container}
      style={{ height: "100%", width: "100%" }}
    />
  );
}

export default memo(TradingViewWidget);
