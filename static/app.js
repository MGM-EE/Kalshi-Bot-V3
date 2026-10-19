"use strict";

(() => {
  const el = Object.fromEntries([...document.querySelectorAll("[id]")].map(node => [node.id, node]));
  const dollars = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const signedDollars = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: "exceptZero" });
  const integer = new Intl.NumberFormat("en-US");
  const localClock = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "America/New_York" });
  const closeClock = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", timeZone: "America/New_York" });
  const marketRows = new Map();
  const conditionRows = new Map();
  const history = [];
  const statusLabels = { live: "Live", connecting: "Connecting", retrying: "Retrying", stale: "Stale" };
  const paperStatusLabels = { warming_up: "Warming up", watching: "Watching", holding: "Holding", paused: "Paused", awaiting_settlement: "Awaiting settlement" };
  const paperEventQueue = [];
  let snapshot = null;
  let frame = null;
  let connectionState = "connecting";
  let snapshotReceivedAt = 0;
  let lastObservation = null;
  let latestSnapshotTime = 0;
  let paperInitialized = false;
  let lastPaperEventId = 0;
  let lastPaperEvent = null;
  let activePaperEvent = null;
  let paperEventTimer = null;
  const exitControlNames = ["hold_to_settlement", "exit_on_stop_loss", "exit_on_reversal", "exit_on_max_hold", "exit_before_expiry"];
  const settingNames = ["min_buy_cents", "max_buy_cents", "min_sell_cents", "dynamic_min_cents", "dynamic_max_cents", "contracts", "dynamic_enabled", "toward_strike_only", "entries_enabled", "stop_loss_cents", "max_spread_cents", "max_hold_seconds", "cooldown_seconds", "min_momentum_bps", "min_flow_percent", "floor_distance_enabled", "max_above_floor_percent", "max_below_floor_percent", ...exitControlNames];
  const settingInputs = Object.fromEntries(settingNames.map(name => [name, el["settings-form"].elements.namedItem(name)]));
  const settingsState = { loaded: false, attempted: false, available: false, busy: false, saving: false, revision: null, saved: null, conflict: false, applyPosition: null };

  function setText(node, value) {
    if (node.textContent !== value) node.textContent = value;
  }

  function numeric(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function formSettings() {
    return Object.fromEntries(settingNames.map(name => {
      const input = settingInputs[name];
      return [name, input.type === "checkbox" ? input.checked : input.value.trim() === "" ? null : Number(input.value)];
    }));
  }

  function settingsDirty() {
    const current = formSettings();
    return settingsState.saved !== null && settingNames.some(name => current[name] !== settingsState.saved[name]);
  }

  function positionIdentity(position = snapshot?.paper?.position) {
    return position && typeof position.ticker === "string" && numeric(position.opened_at) ? { ticker: position.ticker, opened_at: position.opened_at } : null;
  }

  function applyingExitControls() {
    return el["setting-apply-open"].checked && settingsState.applyPosition !== null;
  }

  function renderOpenTradeControls(position) {
    const identity = positionIdentity(position);
    const controls = position?.exit_controls;
    el["settings-open-policy"].hidden = !position;
    if (position) {
      const complete = controls && exitControlNames.every(name => typeof controls[name] === "boolean");
      const early = complete && !controls.hold_to_settlement && exitControlNames.slice(1).some(name => controls[name]);
      setText(el["settings-open-policy"], !complete ? "Open trade: saved exit rules" : early ? "Open trade: early exits enabled" : "Open trade: profit target or settlement");
      el["settings-open-policy"].title = complete ? `Profit targets: active. Hold to settlement: ${controls.hold_to_settlement ? "on" : "off"}. ${[["Stop loss", "exit_on_stop_loss"], ["Momentum reversal", "exit_on_reversal"], ["Maximum hold", "exit_on_max_hold"], ["Before expiry", "exit_before_expiry"]].map(([label, name]) => `${label}: ${!controls.hold_to_settlement && controls[name] ? "on" : "off"}`).join(". ")}.` : "Waiting for the open trade’s saved exit controls.";
    }
    const showApply = Boolean(position || applyingExitControls());
    const visibilityChanged = el["settings-apply-open"].hidden !== !showApply;
    el["settings-apply-open"].hidden = !showApply;
    const selected = settingsState.applyPosition;
    const stale = applyingExitControls() && (!identity || identity.ticker !== selected.ticker || identity.opened_at !== selected.opened_at);
    el["settings-apply-open"].dataset.stale = String(stale);
    setText(el["settings-apply-open-note"], stale ? "The selected trade changed or closed. Uncheck to save for future entries only; check again to select the current trade." : "Only the five exit switches change. Profit prices, stop-loss distance and maximum hold time stay with the open trade.");
    if (visibilityChanged) updateSettingsControls();
  }

  function settingsMessage(message, state = "") {
    setText(el["settings-status"], message);
    el["settings-status"].dataset.state = state;
  }

  function updateSettingsControls() {
    el["settings-fields"].disabled = settingsState.busy || !settingsState.loaded || !settingsState.available;
    const dynamic = settingInputs.dynamic_enabled.checked;
    el["dynamic-range"].setAttribute("aria-disabled", String(!dynamic));
    settingInputs.dynamic_min_cents.disabled = !dynamic;
    settingInputs.dynamic_max_cents.disabled = !dynamic;
    const band = settingInputs.floor_distance_enabled.checked;
    el["floor-band-range"].setAttribute("aria-disabled", String(!band));
    settingInputs.max_above_floor_percent.disabled = !band;
    settingInputs.max_below_floor_percent.disabled = !band;
    renderFloorBandPreview();
    const hold = settingInputs.hold_to_settlement.checked;
    for (const name of exitControlNames.slice(1)) settingInputs[name].disabled = hold;
    el["settings-early-exits"].setAttribute("aria-disabled", String(hold));
    settingInputs.stop_loss_cents.disabled = hold || !settingInputs.exit_on_stop_loss.checked;
    settingInputs.max_hold_seconds.disabled = hold || !settingInputs.exit_on_max_hold.checked;
    el["setting-apply-open"].disabled = !positionIdentity() && !el["setting-apply-open"].checked;
    el["settings-save"].disabled = settingsState.busy || !settingsState.loaded || !settingsState.available || settingsState.conflict || !settingsDirty() && !applyingExitControls();
    setText(el["settings-save"], settingsState.saving ? "Saving…" : "Save settings");
    el["settings-reload"].disabled = settingsState.busy;
  }

  function renderFloorBandPreview() {
    let text = "0.10 means 0.10%. New entries only; exits are unchanged.";
    if (settingInputs.floor_distance_enabled.checked) {
      const reference = snapshot?.reference;
      const below = settingInputs.max_below_floor_percent.valueAsNumber;
      const above = settingInputs.max_above_floor_percent.valueAsNumber;
      if (!numeric(reference?.strike_price) || reference.strike_price <= 0 || reference.strike_source !== "kalshi.floor_strike") {
        text = "Waiting for a verified Kalshi floor. Entries wait while this filter is enabled.";
      } else if (numeric(below) && numeric(above) && below >= 0 && above >= 0 && below <= 100 && above <= 100) {
        text = `Preview: BTC ${money(reference.strike_price * (1 - below / 100))} – ${money(reference.strike_price * (1 + above / 100))}. Save to apply to new entries.`;
      } else {
        text = "Enter percentages from 0 to 100. Example: 0.10 means 0.10%.";
      }
    }
    setText(el["settings-floor-band-preview"], text);
  }

  function clearSettingsErrors() {
    for (const input of Object.values(settingInputs)) input.removeAttribute("aria-invalid");
  }

  function settingsError(message, fields = {}) {
    for (const name of Object.keys(fields)) settingInputs[name]?.setAttribute("aria-invalid", "true");
    settingsMessage(message, "error");
  }

  function validateSettings(settings) {
    for (const name of settingNames) {
      const input = settingInputs[name];
      if (input.type === "checkbox") continue;
      const value = settings[name];
      if (!numeric(value) || value < Number(input.min) || value > Number(input.max) || !input.disabled && !input.checkValidity()) {
        const label = input.closest("label").querySelector("span").textContent.trim();
        return { message: `Check ${label}: enter a value from ${input.min} to ${input.max}.`, fields: { [name]: true } };
      }
    }
    if (settings.min_buy_cents > settings.max_buy_cents) return { message: "Minimum buy must be at or below maximum buy.", fields: { min_buy_cents: true, max_buy_cents: true } };
    if (settings.max_buy_cents >= settings.min_sell_cents) return { message: "Maximum buy must be below the minimum profit-sale price.", fields: { max_buy_cents: true, min_sell_cents: true } };
    if (settings.dynamic_enabled && settings.min_sell_cents > settings.dynamic_min_cents) return { message: "The dynamic range must start at or above the minimum profit-sale price.", fields: { min_sell_cents: true, dynamic_min_cents: true } };
    if (settings.dynamic_enabled && settings.dynamic_min_cents > settings.dynamic_max_cents) return { message: "The dynamic range must run from a lower price to a higher price, up to 95¢.", fields: { dynamic_min_cents: true, dynamic_max_cents: true } };
    return null;
  }

  async function settingsRequest(options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch("/api/paper/settings", { cache: "no-store", ...options, signal: controller.signal });
      let body;
      try { body = await response.json(); } catch { throw new Error("Settings service returned an unreadable response. Try reloading."); }
      if (!response.ok) {
        const error = new Error(typeof body.error === "string" ? body.error : "Settings could not be saved. Try again.");
        error.status = response.status;
        error.fields = body.fields && typeof body.fields === "object" ? body.fields : {};
        throw error;
      }
      if (!body.settings || !Number.isSafeInteger(body.revision) || typeof body.available !== "boolean" || settingNames.some(name => typeof body.settings[name] !== (settingInputs[name].type === "checkbox" ? "boolean" : "number") || typeof body.settings[name] === "number" && !Number.isFinite(body.settings[name]))) {
        throw new Error("Settings service returned an incomplete response. Try reloading.");
      }
      return body;
    } finally {
      clearTimeout(timeout);
    }
  }

  function acceptSettings(body) {
    settingsState.loaded = true;
    settingsState.available = body.available;
    settingsState.revision = body.revision;
    settingsState.saved = Object.fromEntries(settingNames.map(name => [name, body.settings[name]]));
    settingsState.conflict = false;
    settingsState.applyPosition = null;
    el["setting-apply-open"].checked = false;
    for (const name of settingNames) {
      const input = settingInputs[name];
      if (input.type === "checkbox") input.checked = body.settings[name];
      else input.value = String(body.settings[name]);
    }
    clearSettingsErrors();
    el["settings-reload"].hidden = false;
    setText(el["settings-reload"], "Reload saved settings");
    renderOpenTradeControls(snapshot?.paper?.position);
  }

  async function loadSettings() {
    if (settingsState.busy) return;
    settingsState.attempted = true;
    settingsState.busy = true;
    settingsMessage("Loading saved settings…");
    updateSettingsControls();
    try {
      const body = await settingsRequest();
      acceptSettings(body);
      settingsMessage(body.available ? "Saved settings loaded." : "Settings are unavailable. Reload after paper trading is available.", body.available ? "saved" : "error");
    } catch (error) {
      settingsError(error.name === "AbortError" ? "Loading timed out. Retry when the app is connected." : error.message);
      el["settings-reload"].hidden = false;
      setText(el["settings-reload"], settingsState.loaded ? "Reload saved settings" : "Retry loading settings");
    } finally {
      settingsState.busy = false;
      updateSettingsControls();
    }
  }

  async function saveSettings(event) {
    event.preventDefault();
    if (settingsState.busy || !settingsState.loaded || !settingsState.available || settingsState.conflict || !settingsDirty() && !applyingExitControls()) return;
    clearSettingsErrors();
    const settings = formSettings();
    const invalid = validateSettings(settings);
    if (invalid) {
      settingsError(invalid.message, invalid.fields);
      const input = settingInputs[Object.keys(invalid.fields)[0]];
      const advanced = input?.closest(".advanced-settings");
      if (advanced) advanced.open = true;
      input?.focus();
      return;
    }
    settingsState.busy = true;
    settingsState.saving = true;
    settingsMessage("Saving settings…");
    updateSettingsControls();
    try {
      const payload = { settings, revision: settingsState.revision };
      if (applyingExitControls()) payload.apply_exit_controls_to_position = { ...settingsState.applyPosition };
      const body = await settingsRequest({ method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      acceptSettings(body);
      settingsMessage(body.applied_to_position === true ? "Saved exit controls for the open trade; other changes apply next entry." : "Saved. The next entry will use these settings.", "saved");
    } catch (error) {
      if (error.status === 409) {
        settingsState.conflict = true;
        settingsError(`${error.message} Your edits are still here. Reload saved settings before trying again.`);
        el["settings-reload"].hidden = false;
      } else {
        settingsError(error.name === "AbortError" ? "Saving timed out. Your edits are still here. Reload saved settings to check whether they were saved." : error.message, error.fields);
      }
    } finally {
      settingsState.busy = false;
      settingsState.saving = false;
      updateSettingsControls();
    }
  }

  function money(value) {
    return numeric(value) ? dollars.format(value) : "—";
  }

  function cents(value) {
    return numeric(value) ? `${(value * 100).toLocaleString("en-US", { maximumFractionDigits: 1 })}¢` : "—";
  }

  function validPaperEvent(event) {
    return event && Number.isSafeInteger(event.id) && event.id > 0 && ["entry", "exit", "settlement"].includes(event.type) && ["up", "down"].includes(event.side);
  }

  function resetPaperNotifications() {
    paperEventQueue.length = 0;
    activePaperEvent = null;
    if (paperEventTimer !== null) clearTimeout(paperEventTimer);
    paperEventTimer = null;
    el["paper-panel"].classList.remove("paper-flash-green", "paper-flash-red");
  }

  function collectPaperEvents(paper) {
    if (!paper?.enabled) return;
    const candidates = (Array.isArray(paper.events) ? paper.events.slice(-20) : []).filter(validPaperEvent);
    if (validPaperEvent(paper.last_event)) candidates.push(paper.last_event);
    const eventsById = new Map(candidates.map(event => [event.id, event]));
    const events = [...eventsById.values()].sort((a, b) => a.id - b.id);
    const latest = events.at(-1) || null;
    if (!paperInitialized) {
      paperInitialized = true;
      lastPaperEventId = latest?.id || 0;
      lastPaperEvent = latest;
      return; // Existing trades are history, not new activity on page load.
    }
    // A restarted paper session can have a fresh event counter.
    if (!latest && paper.last_event === null) {
      lastPaperEventId = 0;
      lastPaperEvent = null;
      resetPaperNotifications();
      return;
    }
    if (latest && latest.id < lastPaperEventId && Date.parse(latest.at) > Date.parse(lastPaperEvent?.at)) {
      lastPaperEventId = 0;
      resetPaperNotifications();
    }
    for (const event of events) {
      if (event.id <= lastPaperEventId) continue;
      paperEventQueue.push(event);
      lastPaperEventId = event.id;
      lastPaperEvent = event;
    }
    if (paperEventQueue.length > 8) paperEventQueue.splice(0, paperEventQueue.length - 8);
  }

  function paperSide(side) {
    return side === "up" ? "Up" : "Down";
  }

  function paperEventText(event) {
    const action = event.type === "entry" ? "Bought" : event.type === "settlement" ? "Settled" : "Sold";
    const quantity = numeric(event.contracts) ? ` ×${integer.format(event.contracts)}` : "";
    const price = numeric(event.price) ? ` at ${cents(event.price)}` : "";
    const pnl = event.type !== "entry" && numeric(event.pnl) ? ` · ${signedDollars.format(event.pnl)}` : "";
    return `${action} ${paperSide(event.side)}${quantity}${price}${pnl}`;
  }

  function paperOutcome(event) {
    if (event.type === "entry") return "green";
    return numeric(event.pnl) ? event.pnl < 0 ? "red" : "green" : "";
  }

  function startPaperNotification() {
    if (activePaperEvent || paperEventTimer !== null || !paperEventQueue.length || connectionState !== "live") return;
    activePaperEvent = paperEventQueue.shift();
    const outcome = paperOutcome(activePaperEvent);
    if (outcome) el["paper-panel"].classList.add(`paper-flash-${outcome}`);
    paperEventTimer = setTimeout(() => {
      el["paper-panel"].classList.remove("paper-flash-green", "paper-flash-red");
      activePaperEvent = null;
      // Let the previous animation finish before the next queued trade.
      paperEventTimer = setTimeout(() => {
        paperEventTimer = null;
        scheduleRender();
      }, 40);
      scheduleRender();
    }, 1500);
  }

  function renderPaper() {
    const paper = snapshot?.paper;
    if (!paper) return;
    el["paper-panel"].hidden = !paper.enabled;
    if (!paper.enabled) return;
    const connected = connectionState === "live";
    const feedsLive = feedState(snapshot.feeds.kraken) === "live" && feedState(snapshot.feeds.kalshi) === "live";
    const state = !connected || !feedsLive && paper.status !== "awaiting_settlement" ? "paused" : paper.status;
    el["paper-panel"].dataset.status = state;
    setText(el["paper-status"], !connected ? connectionState === "retrying" ? "Reconnecting" : "Connecting" : paperStatusLabels[state] || "Watching");
    const position = paper.position;
    renderOpenTradeControls(position);
    const entriesPaused = connected && feedsLive && paper.config?.entries_enabled === false;
    let positionText = entriesPaused ? "New paper buys paused" : state === "warming_up" ? "Collecting a little history" : state === "paused" ? "Waiting for fresh data" : "Waiting for a setup";
    if (position) positionText = `Holding ${paperSide(position.side)} ×${integer.format(position.contracts)}`;
    else if (lastPaperEvent && lastPaperEvent.type !== "entry") positionText = `Last exit · ${paperSide(lastPaperEvent.side)}`;
    setText(el["paper-position"], positionText);
    el["paper-position"].title = position?.ticker || lastPaperEvent?.ticker || "Simulated trades only";
    if (connected && feedsLive) startPaperNotification();
    const displayEvent = activePaperEvent || lastPaperEvent;
    let action = displayEvent ? `${activePaperEvent ? "" : "Last trade · "}${paperEventText(displayEvent)}` : "Simulated trades · no real orders";
    if (!connected) action = "Waiting for connection · showing last known state";
    else if (!feedsLive && state !== "awaiting_settlement") action = "Waiting for fresh feeds · paper entries paused";
    else if (!displayEvent && typeof paper.message === "string" && paper.message) action = paper.message;
    setText(el["paper-action"], action);
    el["paper-panel"].dataset.outcome = connected && feedsLive && displayEvent ? paperOutcome(displayEvent) : "";
    if (!connected || !feedsLive) el["paper-panel"].classList.remove("paper-flash-green", "paper-flash-red");
    const target = position && numeric(position.exit_target) ? `Exit ${cents(position.exit_target)} · cap 95¢` : "";
    setText(el["paper-target"], target);
    el["paper-target"].hidden = !target;
    const pnl = numeric(paper.realized_pnl) && (!position || numeric(paper.unrealized_pnl)) ? paper.realized_pnl + (position ? paper.unrealized_pnl : 0) : null;
    setText(el["paper-pnl"], numeric(pnl) ? signedDollars.format(pnl) : "—");
    el["paper-pnl"].dataset.direction = numeric(pnl) ? pnl > 0 ? "positive" : pnl < 0 ? "negative" : "flat" : "";
    el["paper-pnl"].title = position ? "Realized plus open P&L, after estimated fees" : "Realized P&L, after estimated fees";
  }

  function createConditionRow(key) {
    const row = document.createElement("details");
    row.className = "condition-check";
    row.dataset.key = key;
    const summary = document.createElement("summary");
    const dot = document.createElement("span");
    dot.className = "condition-dot";
    dot.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.className = "condition-label";
    const state = document.createElement("span");
    state.className = "condition-state";
    const detail = document.createElement("p");
    detail.className = "condition-detail";
    summary.append(dot, label, state);
    row.append(summary, detail);
    const nodes = { row, summary, label, state, detail };
    conditionRows.set(key, nodes);
    return nodes;
  }

  function renderConditions() {
    const paper = snapshot?.paper;
    el["conditions-panel"].hidden = !paper?.enabled;
    if (!paper?.enabled) return;
    const conditions = paper.conditions;
    const mode = conditions?.mode === "exit" || !conditions && paper.position ? "Exit" : "Entry";
    const side = ["up", "down"].includes(conditions?.side) ? ` · ${paperSide(conditions.side)}` : "";
    setText(el["conditions-heading"], `${mode} conditions${side}`);
    setText(el["conditions-note"], mode === "Exit" ? "Open trade’s rules · exit triggers are alternatives; a sale still needs executable depth. Select a check for details." : "Saved rules · green means a condition is met, not a guaranteed fill. Select a check for details.");
    // Compare server timestamps, then advance with a monotonic clock. The device's
    // wall clock may differ from the server's and must not keep stale checks green.
    const serverTime = Date.parse(snapshot.server_time);
    const checkedAt = numeric(conditions?.updated_at) ? conditions.updated_at * 1000 : NaN;
    const atReceipt = serverTime - checkedAt;
    const sinceReceipt = Math.max(0, performance.now() - snapshotReceivedAt);
    const unavailable = !numeric(atReceipt) || atReceipt < -3000;
    const connected = connectionState === "live";
    const stale = !connected || unavailable || sinceReceipt > 3000 || Math.max(0, atReceipt) + sinceReceipt > 3000;
    el["conditions-panel"].dataset.stale = String(stale);
    const summary = typeof conditions?.summary === "string" && conditions.summary ? conditions.summary : "Waiting for engine checks.";
    setText(el["conditions-summary"], !connected ? "Waiting for connection · checks will refresh automatically." : unavailable ? summary : stale ? "Checks are stale · waiting for fresh data." : summary);
    const stateLabels = { met: "Met", building: "Building", blocked: "Not met", disabled: "Off", stale: "Waiting" };
    const keys = new Set();
    const items = Array.isArray(conditions?.items) ? conditions.items.slice(0, 24) : [];
    for (const item of items) {
      if (!item || typeof item.key !== "string" || keys.has(item.key)) continue;
      keys.add(item.key);
      const nodes = conditionRows.get(item.key) || createConditionRow(item.key);
      const activeState = ["met", "building", "blocked", "disabled"].includes(item.state) ? item.state : "building";
      const state = stale && activeState !== "disabled" ? "stale" : activeState;
      const detail = typeof item.detail === "string" ? item.detail : "No additional detail.";
      nodes.row.dataset.state = state;
      setText(nodes.label, typeof item.label === "string" ? item.label : item.key);
      setText(nodes.state, stateLabels[state]);
      const detailText = state === "stale" ? `Last check (stale): ${detail}` : detail;
      setText(nodes.detail, detailText);
      if (nodes.summary.title !== detailText) nodes.summary.title = detailText;
      const current = el["conditions-list"].children[keys.size - 1];
      if (current !== nodes.row) el["conditions-list"].insertBefore(nodes.row, current || null);
    }
    for (const [key, nodes] of conditionRows) {
      if (!keys.has(key)) {
        nodes.row.remove();
        conditionRows.delete(key);
      }
    }
    el["conditions-empty"].hidden = keys.size > 0;
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
    collectPaperEvents(next.paper);
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
    renderPaper();
    renderConditions();
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
    const reference = snapshot.reference || {};
    renderFloorBandPreview();
    setText(el["settings-strike"], numeric(reference.strike_price) && reference.strike_source === "kalshi.floor_strike" ? `Strike ${money(reference.strike_price)} · trend uses Kraken spot; settlement uses Kalshi’s BRTI reference.` : "Strike unavailable · buys toward the strike wait for a reference.");
    const series = typeof data.series === "string" ? data.series : "KXBTC15M";
    setText(el["kalshi-series"], series === "KXBTC15M" ? `${series} · 15-minute markets` : series);
    renderMarkets(Array.isArray(data.markets) ? data.markets : []);
    setText(el["loop-lag"], numeric(snapshot.metrics.loop_lag_ms) ? `${snapshot.metrics.loop_lag_ms.toFixed(1)} ms` : "—");
    setText(el["update-count"], integer.format((feeds.kraken?.updates || 0) + (feeds.kalshi?.updates || 0)));
  }

  el["trade-settings"].addEventListener("toggle", () => {
    if (el["trade-settings"].open && !settingsState.attempted) loadSettings();
  });
  el["settings-form"].addEventListener("submit", saveSettings);
  el["settings-reload"].addEventListener("click", loadSettings);
  el["settings-form"].addEventListener("input", event => {
    if (event.target === el["setting-apply-open"]) {
      settingsState.applyPosition = event.target.checked ? positionIdentity() : null;
      if (event.target.checked && !settingsState.applyPosition) event.target.checked = false;
      if (!settingsState.conflict) settingsMessage(applyingExitControls() ? "Exit controls will also apply to the selected open trade when saved." : settingsDirty() ? "Unsaved changes." : "Saved settings loaded.", applyingExitControls() || settingsDirty() ? "" : "saved");
      renderOpenTradeControls(snapshot?.paper?.position);
      updateSettingsControls();
      return;
    }
    if (!Object.values(settingInputs).includes(event.target)) return;
    clearSettingsErrors();
    if (!settingsState.conflict) settingsMessage(settingsDirty() || applyingExitControls() ? "Unsaved changes." : "Saved settings loaded.", settingsDirty() || applyingExitControls() ? "" : "saved");
    updateSettingsControls();
  });

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
