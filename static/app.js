"use strict";

(() => {
  const el = Object.fromEntries([...document.querySelectorAll("[id]")].map(node => [node.id, node]));
  const dollars = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const integer = new Intl.NumberFormat("en-US");
  const localClock = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "America/New_York" });
  const closeClock = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", timeZone: "America/New_York" });
  const marketRows = new Map();
  const history = [];
  const statusLabels = { live: "Live", connecting: "Connecting", retrying: "Retrying", stale: "Stale" };
  let snapshot = null;
  let frame = null;
  let connectionState = "connecting";
  let snapshotReceivedAt = 0;
  let lastObservation = null;
  let latestSnapshotTime = 0;

  function setText(node, value) {
    if (node.textContent !== value) node.textContent = value;
  }

  function numeric(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function money(value) {
    return numeric(value) ? dollars.format(value) : "—";
  }

  function cents(value) {
    return numeric(value) ? `${(value * 100).toLocaleString("en-US", { maximumFractionDigits: 1 })}¢` : "—";
  }

  function feedAge(feed) {
    if (numeric(feed?.age_ms)) return Math.max(0, feed.age_ms + performance.now() - snapshotReceivedAt);
    const timestamp = Date.parse(feed?.received_at);
    return Number.isFinite(timestamp) ? Math.max(0, Date.now() - timestamp) : null;
  }

  function age(feed) {
    const milliseconds = feedAge(feed);
    if (milliseconds === null) return "—";
    const seconds = Math.floor(milliseconds / 1000);
    if (seconds < 1) return "just now";
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    return `${Math.floor(seconds / 3600)}h ago`;
  }

  function feedState(feed) {
    const elapsed = feedAge(feed);
    if (elapsed !== null && elapsed > 15000) return "stale";
    if (connectionState === "retrying") return "retrying";
    return feed?.status in statusLabels ? feed.status : "connecting";
  }

  function scheduleRender() {
    if (frame === null) frame = requestAnimationFrame(render);
  }

  function acceptSnapshot(next) {
    if (!next || !next.feeds || !next.metrics) return;
    const timestamp = Date.parse(next.server_time) || 0;
    if (timestamp < latestSnapshotTime) return;
    latestSnapshotTime = timestamp;
    snapshot = next;
    snapshotReceivedAt = performance.now();
    const feed = next.feeds.kraken;
    if (feed?.received_at && feed.received_at !== lastObservation) {
      lastObservation = feed.received_at;
      const quote = feed.data || {};
      const price = numeric(quote.last) ? quote.last : numeric(quote.bid) && numeric(quote.ask) ? (quote.bid + quote.ask) / 2 : null;
      if (price !== null) {
        history.push(price);
        if (history.length > 120) history.shift();
      }
    }
    scheduleRender();
  }

  function renderFeed(name, feed) {
    const state = feedState(feed);
    const status = el[`${name}-status`];
    if (status.dataset.state !== state) status.dataset.state = state;
    setText(status.lastElementChild, statusLabels[state]);
    setText(el[`${name}-age`], age(feed));
    const transport = feed?.transport === "websocket" ? "WebSocket" : feed?.transport === "rest" ? "REST" : "Awaiting feed";
    const latency = numeric(feed?.request_ms) ? ` · RTT ${Math.round(feed.request_ms)} ms` : "";
    setText(el[`${name}-transport`], `${transport}${latency}`);
    const error = typeof feed?.error === "string" ? feed.error : "";
    setText(el[`${name}-error`], error);
    el[`${name}-error`].hidden = !error;
  }

  function renderChart() {
    setText(el["chart-samples"], `${history.length} observation${history.length === 1 ? "" : "s"}`);
    el["chart-empty"].hidden = history.length > 1;
    if (history.length < 2) {
      setText(el["chart-empty"], history.length === 1 ? "Collecting live prices" : "Waiting for live prices");
      return;
    }
    const low = Math.min(...history);
    const high = Math.max(...history);
    const span = Math.max(high - low, Math.abs(high) * 0.00001, 0.01);
    const middle = (high + low) / 2;
    const points = history.map((value, index) => `${(index * 600 / (history.length - 1)).toFixed(2)},${(66 - (value - middle) / span * 100).toFixed(2)}`);
    const line = `M${points.join(" L")}`;
    if (el["chart-line"].getAttribute("d") !== line) {
      el["chart-line"].setAttribute("d", line);
      el["chart-area-path"].setAttribute("d", `${line} L600,132 L0,132 Z`);
    }
  }

  function createMarketRow(ticker) {
    const row = document.createElement("tr");
    const description = document.createElement("td");
    const title = document.createElement("div");
    title.className = "market-title";
    const close = document.createElement("div");
    close.className = "market-close";
    description.append(title, close);
    const bid = document.createElement("td");
    const ask = document.createElement("td");
    const last = document.createElement("td");
    row.append(description, bid, ask, last);
    const nodes = { row, title, close, bid, ask, last };
    marketRows.set(ticker, nodes);
    return nodes;
  }

  function renderMarkets(markets) {
    const currentTickers = new Set();
    markets.forEach((market, index) => {
      if (!market || typeof market.ticker !== "string" || currentTickers.has(market.ticker)) return;
      currentTickers.add(market.ticker);
      const nodes = marketRows.get(market.ticker) || createMarketRow(market.ticker);
      setText(nodes.title, market.title || market.ticker);
      nodes.title.title = market.title || market.ticker;
      const closeTime = Date.parse(market.close_time);
      setText(nodes.close, Number.isFinite(closeTime) ? `Closes ${closeClock.format(closeTime)} ET` : market.ticker);
      setText(nodes.bid, cents(market.yes_bid));
      setText(nodes.ask, cents(market.yes_ask));
      setText(nodes.last, cents(market.last));
      const current = el.markets.children[index];
      if (current !== nodes.row) el.markets.insertBefore(nodes.row, current || null);
    });
    for (const [ticker, nodes] of marketRows) {
      if (!currentTickers.has(ticker)) {
        nodes.row.remove();
        marketRows.delete(ticker);
      }
    }
    el["markets-empty"].hidden = currentTickers.size > 0;
    setText(el["contract-count"], `${currentTickers.size} contract${currentTickers.size === 1 ? "" : "s"}`);
  }

  function render() {
    frame = null;
    setText(el.clock, `${localClock.format(Date.now())} ET`);
    const feeds = snapshot?.feeds || {};
    const feedStates = [feedState(feeds.kraken), feedState(feeds.kalshi)];
    const liveCount = feedStates.filter(state => state === "live").length;
    const connected = connectionState === "live";
    const state = connected ? liveCount === 2 ? "live" : feedStates.some(state => state === "stale" || state === "retrying") ? "retrying" : "connecting" : connectionState;
    el["session-dot"].className = `status-dot ${state}`;
    setText(el["session-label"], connected ? liveCount === 2 ? "All feeds live" : `${liveCount} of 2 feeds live` : connectionState === "retrying" ? "Reconnecting" : "Connecting");
    setText(el["system-state"], connected ? "Live connection · automatic updates" : connectionState === "retrying" ? "Connection interrupted · retrying" : "Establishing connection");
    if (!snapshot) return;
    renderFeed("kraken", feeds.kraken);
    renderFeed("kalshi", feeds.kalshi);
    const quote = feeds.kraken?.data || {};
    setText(el["kraken-symbol"], typeof quote.symbol === "string" ? quote.symbol.replace("/", " / ") : "BTC / USD");
    const priceText = money(quote.last);
    if (el["kraken-last"].firstChild.nodeValue !== priceText) el["kraken-last"].firstChild.nodeValue = priceText;
    setText(el["kraken-bid"], money(quote.bid));
    setText(el["kraken-ask"], money(quote.ask));
    setText(el["kraken-spread"], numeric(quote.bid) && numeric(quote.ask) ? money(quote.ask - quote.bid) : "—");
    renderChart();
    const data = feeds.kalshi?.data || {};
    const series = typeof data.series === "string" ? data.series : "KXBTC15M";
    setText(el["kalshi-series"], series === "KXBTC15M" ? `${series} · 15-minute markets` : series);
    renderMarkets(Array.isArray(data.markets) ? data.markets : []);
    setText(el["loop-lag"], numeric(snapshot.metrics.loop_lag_ms) ? `${snapshot.metrics.loop_lag_ms.toFixed(1)} ms` : "—");
    setText(el["update-count"], integer.format((feeds.kraken?.updates || 0) + (feeds.kalshi?.updates || 0)));
  }

  fetch("/api/snapshot", { cache: "no-store" })
    .then(response => { if (!response.ok) throw new Error("Snapshot unavailable"); return response.json(); })
    .then(acceptSnapshot)
    .catch(() => { /* EventSource retries independently when the server is available. */ });

  const events = new EventSource("/api/events");
  events.onopen = () => { connectionState = "live"; scheduleRender(); };
  events.onerror = () => { connectionState = "retrying"; scheduleRender(); };
  events.addEventListener("snapshot", event => {
    try { acceptSnapshot(JSON.parse(event.data)); } catch { /* Keep the latest valid snapshot. */ }
  });
  setInterval(() => { if (!document.hidden) scheduleRender(); }, 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) scheduleRender(); });
  scheduleRender();
})();
