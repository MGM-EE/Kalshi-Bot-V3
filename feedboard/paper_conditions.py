"""Read-only explanations of the paper engine's existing entry and exit gates.

These values are UI diagnostics, not orders, new trading rules, or persisted state.
All inputs come from the last engine step; observing this view never samples a feed.
"""
from __future__ import annotations

from .paper import _fresh, _levels, _num, _timestamp, taker_fee
from .entry_filters import entry_requirements, in_entry_window


def _item(key, label, state, detail):
    return {"key": key, "label": label, "state": state, "detail": detail}


def _cents(value):
    return f"{value * 100:.1f}¢"


def _progress(value, threshold):
    return "met" if value >= threshold else "building" if value > 0 else "blocked"


def _signal_for_rules(signal, config):
    if signal is None:
        return None
    # A setting change or completed exit can change the active rules between
    # observation and rendering. Reuse returns/volatility without observing again.
    signal = dict(signal)
    if "volatility" in signal:
        signal["floor5"] = max(config.min_momentum_bps, signal["volatility"] * .6)
        signal["floor20"] = max(config.min_momentum_bps * 4 / 3, signal["volatility"] * 1.5)
    return signal


def build_conditions(engine, context):
    mode = "exit" if engine.position else "entry"
    if context is None:
        return {"mode": mode, "side": engine.position["side"] if engine.position else None,
                "summary": "Waiting for the first market update.", "updated_at": None, "items": []}
    if engine.position:
        return _exit_conditions(engine, context)
    return _entry_conditions(engine, context)


def _entry_conditions(engine, context):
    config = engine.config
    book, feeds = context["book"], context["feeds"]
    kraken, kalshi = feeds.get("kraken", {}), feeds.get("kalshi", {})
    signal = _signal_for_rules(context["signal"], config)
    flow = context["flow"]
    wall_time, now = context["wall_time"], context["now"]
    ticker = book.get("ticker")
    data = kraken.get("data", {})
    strike = _num(book.get("strike_price"))
    verified = strike is not None and strike > 0 and book.get("strike_source") == "kalshi.floor_strike"
    quote_bid, quote_ask = _num(data.get("bid")), _num(data.get("ask"))
    midpoint = signal["midpoint"] if signal else ((quote_bid + quote_ask) / 2
               if quote_bid is not None and quote_ask is not None and 0 < quote_bid <= quote_ask else None)
    if config.require_toward_strike and verified and midpoint is not None and midpoint != strike:
        side = "up" if midpoint < strike else "down"
    elif signal:
        score = signal["five"] / signal["floor5"] + signal["twenty"] / signal["floor20"]
        side = "up" if score >= 0 else "down"
    else:
        side = None
    range_context = engine._range_context.view()
    required = entry_requirements(config, signal, side, range_context) if signal else None
    direction = 1 if side == "up" else -1
    yes, no = _levels(book.get("yes")), _levels(book.get("no"))
    bids = (yes if side == "up" else no) if side else []
    asks = sorted((round(1 - price, 6), size) for price, size in
                  ((no if side == "up" else yes) if side else []))
    ask, bid = (asks[0][0] if asks else None), (bids[0][0] if bids else None)
    items = []
    add = lambda key, label, state, detail: items.append(_item(key, label, state, detail))
    supported = (isinstance(ticker, str) and ticker.startswith(config.series + "-"))
    transport = kraken.get("transport") == "websocket" and data.get("symbol") == config.symbol
    valid_quote = quote_bid is not None and quote_ask is not None and 0 < quote_bid <= quote_ask
    fresh = _fresh(kalshi) and _fresh(kraken) and _fresh(book)
    wrong_source = ((kraken.get("transport") not in (None, "websocket"))
                    or data.get("symbol") not in (None, config.symbol)
                    or (ticker is not None and not supported))
    add("data", "Live market data", "met" if fresh and supported and transport and valid_quote else
        "blocked" if wrong_source else "building",
        "Fresh Kalshi book and BTC/USD WebSocket; Bitcoin 15-minute market." if fresh and supported and transport and valid_quote
        else "Requires fresh (≤3s) Kalshi/book and BTC/USD WebSocket data for KXBTC15M.")
    duration = max(0., now - engine.samples[0][0]) if engine.samples else 0.
    add("warmup", "Momentum warmup", "met" if signal else "building",
        f"{min(duration, config.warmup_seconds):.1f}/{config.warmup_seconds:g}s; "
        f"{len(engine.samples)} distinct quotes (at least 5 required).")
    close = _timestamp(book.get("close_time"))
    remaining = close - wall_time if close is not None else None
    market_ok = (remaining is not None and remaining >= 90 and book.get("market_status") in ("open", "active")
                 and book.get("settlement_result") not in ("yes", "no")
                 and in_entry_window(config, close, wall_time))
    timing = (f"{remaining:.0f}s left; " if remaining is not None else "Close time unavailable; ")
    add("market", "Market / entry timing", "met" if market_ok else "blocked", timing +
        "open market and ≥90s remaining required. " +
        (f"Entries from minute {config.entry_start_minute:g} inclusive to {config.entry_end_minute:g} exclusive."
         if config.entry_window_enabled else "Custom entry window is off."))
    cooldown = max(0., engine.cooldown_until - wall_time)
    add("cooldown", "Entries / cooldown", "blocked" if not config.entries_enabled else
        "building" if cooldown else "met", "New entries paused." if not config.entries_enabled else
        f"{cooldown:.1f}s until entries resume." if cooldown else "Entries enabled; cooldown complete.")
    if not config.require_toward_strike:
        add("toward_strike", "Toward the floor", "disabled", "Toward-strike filter is off.")
    elif not verified or midpoint is None or side is None:
        add("toward_strike", "Toward the floor", "building", "Waiting for verified Kalshi floor and BTC price.")
    else:
        toward = direction * (strike - midpoint) > 0
        add("toward_strike", "Toward the floor", "met" if toward else "blocked",
            f"{side.upper()} requires BTC {'below' if side == 'up' else 'above'} the verified ${strike:,.2f} floor; "
            f"BTC ${midpoint:,.2f}. Momentum must also agree.")
    if not config.floor_distance_enabled:
        add("floor_band", "Floor distance", "disabled", "Percentage band is off.")
    elif not verified or midpoint is None:
        add("floor_band", "Floor distance", "building", "Waiting for verified Kalshi floor and BTC price.")
    else:
        distance = (midpoint - strike) / strike * 100
        within = (-config.max_below_floor_percent - 1e-10 <= distance <= config.max_above_floor_percent + 1e-10)
        add("floor_band", "Floor distance", "met" if within else "blocked",
            f"BTC {distance:+.4f}%; allowed −{config.max_below_floor_percent:g}% to +{config.max_above_floor_percent:g}%.")
    if signal is None or side is None:
        add("momentum", "5s / 20s momentum", "building", "Waiting for warmup; both periods must agree on one side.")
    else:
        five, twenty = direction * signal["five"], direction * signal["twenty"]
        state = "met" if five >= required["momentum5_bps"] and twenty >= required["momentum20_bps"] else (
            "building" if five > 0 and twenty > 0 else "blocked")
        add("momentum", "5s / 20s momentum", state,
            f"{side.upper()}-aligned: {five:+.2f}/{required['momentum5_bps']:.2f} bps (5s), "
            f"{twenty:+.2f}/{required['momentum20_bps']:.2f} bps (20s). Range factor {required['range_factor']:.2f}×.")
    if flow is None or side is None:
        add("flow", "Aligned trade flow", "building",
            "Requires ≥3 trades, ≥15s window, ≤3s age, positive volume, and a candidate side.")
        add("large_prints", "Large prints", "building", "Waiting for valid fresh trade flow and a candidate side.")
    else:
        aligned, large = direction * flow["signed"], direction * flow["large"]
        flow_floor = required["flow_imbalance"] if required else config.flow_imbalance
        add("flow", "Aligned trade flow", _progress(aligned, flow_floor),
            f"{side.upper()}-aligned {aligned * 100:+.1f}% / ≥{flow_floor * 100:g}%; "
            "fresh ≥15s window with ≥3 trades.")
        add("large_prints", "Large prints", "met" if large >= -.35 else "blocked",
            f"{side.upper()}-aligned large-print imbalance {large * 100:+.1f}% / ≥−35%.")
    if ask is None:
        add("buy_price", "Buy price", "building", "Waiting for a candidate side and visible asks.")
    else:
        add("buy_price", "Buy price", "met" if config.min_entry_price <= ask <= config.max_entry_price else "blocked",
            f"{side.upper()} ask {_cents(ask)}; allowed {_cents(config.min_entry_price)}–{_cents(config.max_entry_price)}.")
    spread = ask - bid if ask is not None and bid is not None else None
    add("spread", "Spread", "building" if spread is None else "met" if -1e-9 <= spread <= config.max_spread + 1e-9 else "blocked",
        "Waiting for both sides of the candidate book." if spread is None else
        f"{_cents(spread)} / ≤{_cents(config.max_spread)}; crossed books are rejected.")
    cap = min(ask + .01, config.max_entry_price) if ask is not None else None
    fills = engine._sweep(asks, config.contracts, ceiling=cap) if ask is not None else []
    quantity = sum(size for _, size in fills)
    add("depth", "Executable buy depth", "building" if ask is None else "met" if quantity == config.contracts else "blocked",
        "Waiting for candidate asks." if cap is None else
        f"{quantity}/{config.contracts} contracts at ≤{_cents(cap)} (1¢ slippage and buy cap).")
    cost = (sum(price * size for price, size in fills)
            + sum(taker_fee(size, price) for price, size in fills))
    add("cash", "Cash including fees", "building" if quantity != config.contracts else "met" if cost <= engine.cash else "blocked",
        f"Available ${engine.cash:.2f}; " + (f"full buy + estimated fees ${cost:.2f}." if quantity == config.contracts
                                          else "full-depth cost unavailable."))
    revision = supported and engine._available_revision(book)
    add("book_revision", "Unused book update", "met" if revision else "building",
        "Fresh book revision available for a simulated fill." if revision else
        "Waiting for a new book revision; previously used depth cannot be filled twice.")
    quality = engine._entry_quality.view()
    factor = required["range_factor"] if required else 1.
    range_detail = (f"Observed ${range_context['lower']:,.2f}–${range_context['upper']:,.2f}; "
                    f"{range_context['regime']}; {side or 'candidate'} factor {factor:.2f}×. "
                    if range_context.get("ready") else "Collecting a rolling range; neutral thresholds during warmup. ")
    add("range_bias", "Rolling range bias", "met" if config.range_bias_enabled else "disabled",
        range_detail + ("Entry momentum/flow only; other gates and exits still apply."
                        if config.range_bias_enabled else "Observation only; enable in settings to test entry weights."))
    efficiency = quality["efficiency"]
    if not config.choppiness_enabled:
        add("choppiness", "Trend quality", "disabled", "Choppiness filter is off.")
    elif efficiency is None:
        add("choppiness", "Trend quality", "building",
            f"{min(60, quality['coverage_seconds']):.1f}/60s of fresh BTC trend history; gaps restart warmup.")
    else:
        add("choppiness", "Trend quality", _progress(efficiency, config.min_trend_efficiency),
            f"60s path efficiency {efficiency * 100:.1f}% / ≥{config.min_trend_efficiency * 100:g}%; "
            "straightness, not settlement probability.")
    if not config.confirmation_enabled:
        add("confirmation", "Signal confirmation", "disabled", "Signal confirmation is off.")
    else:
        same = quality["confirmation_side"] == side and quality["confirmation_ticker"] == ticker
        elapsed = quality["confirmation_elapsed_seconds"] if same else 0.
        ready = (same and elapsed >= config.confirmation_seconds
                 and quality["confirmation_new_quote"] and quality["confirmation_new_book"])
        add("confirmation", "Signal confirmation", "met" if ready else "building" if same else "blocked",
            f"{elapsed:.1f}/{config.confirmation_seconds:g}s of all entry checks met on the same side; "
            "requires new BTC and book updates. Failures, changes, and stale data restart the timer.")
    active = [item for item in items if item["state"] != "disabled"]
    all_met = all(item["state"] == "met" for item in active)
    summary = (f"{side.upper()} entry conditions met." if all_met and side else
               f"Watching {side.upper()} entry conditions." if side else "Waiting for an entry candidate.")
    return {"mode": "entry", "side": side, "summary": summary, "updated_at": wall_time, "items": items}


def _exit_conditions(engine, context):
    position, book = engine.position, context["book"]
    rules = engine._position_config()
    signal, flow = _signal_for_rules(context["signal"], rules), context["flow"]
    wall_time = context["wall_time"]
    side, direction = position["side"], 1 if position["side"] == "up" else -1
    same = book.get("ticker") == position["ticker"]
    bids = _levels(book.get("yes" if side == "up" else "no"))
    close = _timestamp(position["close_time"])
    remaining = close - wall_time
    closed = remaining <= 0 or (same and book.get("market_status") in ("closed", "settled", "finalized"))
    settlement = same and book.get("settlement_result") in ("yes", "no")
    fresh = same and _fresh(book)
    tradable = book.get("market_status") in ("open", "active")
    observable = fresh and tradable and bool(bids) and not closed
    bid = bids[0][0] if observable else None
    target = position["exit_target"]
    held = max(0., wall_time - position["opened_at"])
    revision = same and engine._available_revision(book)
    items = []
    add = lambda key, label, state, detail: items.append(_item(key, label, state, detail))
    add("exit_book", "Executable sell book", "met" if observable else "blocked" if not same else "building",
        f"Fresh matching {side.upper()} bids; market open." if observable else
        "Book belongs to a different contract." if not same else
        "Waiting for fresh matching bids and an open market; settlement is checked separately.")
    add("book_revision", "Unused book update", "met" if same and revision else "building",
        "New depth available." if same and revision else "Waiting for a new matching book revision.")
    add("profit_target", "Dynamic profit target" if rules.dynamic_exit_enabled else "Fixed profit target",
        "building" if bid is None else "met" if bid >= target else "building" if bid > position["entry_price"] else "blocked",
        f"Target {_cents(target)}; " + (f"bid {_cents(bid)}." if bid is not None else "waiting for an executable bid.") +
        (" Uses this position's saved rules." if rules.dynamic_exit_enabled else ""))
    add("hard_95c", "Absolute 95¢ exit", "building" if bid is None else "met" if bid >= .95 else
        "building" if bid > position["entry_price"] else "blocked",
        "Always enabled; " + (f"bid {_cents(bid)} / ≥95¢." if bid is not None else "waiting for executable bids."))
    stop = position["entry_price"] - rules.stop_loss
    stop_enabled = not rules.hold_to_settlement and rules.exit_on_stop_loss
    stop_hit = bid is not None and bid <= stop
    add("stop_loss", "Stop loss", "disabled" if not stop_enabled else "building" if bid is None else
        "met" if stop_hit else "building" if bid < position["entry_price"] else "blocked",
        "Disabled by hold-to-settlement or this position's stop-loss toggle." if not stop_enabled else
        f"Triggers at ≤{_cents(stop)}; " + (f"bid {_cents(bid)}." if bid is not None else "waiting for fresh bids."))
    reversal_enabled = not rules.hold_to_settlement and rules.exit_on_reversal
    reversal_hit = bool(signal and flow and direction * signal["five"] <= -signal["floor5"]
                        and direction * flow["signed"] <= -rules.flow_imbalance)
    if not reversal_enabled:
        reversal_state, reversal_detail = "disabled", "Disabled by hold-to-settlement or this position's reversal toggle."
    elif not signal or not flow:
        reversal_state, reversal_detail = "building", "Requires warmed-up BTC momentum and fresh trade flow."
    else:
        five, aligned = direction * signal["five"], direction * flow["signed"]
        reversal_state = "met" if reversal_hit else "building" if five < 0 or aligned < 0 else "blocked"
        reversal_detail = (f"{side.upper()}-aligned 5s {five:+.2f} bps / ≤−{signal['floor5']:.2f}; "
                           f"flow {aligned * 100:+.1f}% / ≤−{rules.flow_imbalance * 100:g}%.")
    add("reversal", "Momentum reversal", reversal_state, reversal_detail)
    hold_enabled = not rules.hold_to_settlement and rules.exit_on_max_hold
    hold_hit = held >= rules.max_hold_seconds
    add("max_hold", "Maximum hold time", "disabled" if not hold_enabled else "met" if hold_hit else "building",
        "Disabled by hold-to-settlement or this position's hold-time toggle." if not hold_enabled else
        f"Held {held:.1f}s / {rules.max_hold_seconds:g}s.")
    expiry_enabled = not rules.hold_to_settlement and rules.exit_before_expiry
    expiry_hit = remaining <= 30
    add("expiry", "Before-expiry exit", "disabled" if not expiry_enabled else "met" if expiry_hit else
        "building" if remaining <= 90 else "blocked",
        "Disabled by hold-to-settlement or this position's expiry toggle." if not expiry_enabled else
        f"{remaining:.0f}s remaining / triggers at ≤30s while market is open.")
    add("settlement", "Official settlement", "met" if settlement else "building" if closed else "blocked",
        "Verified Kalshi settlement result available." if settlement else
        "Market closed; waiting for Kalshi's official result." if closed else f"Market open; {remaining:.0f}s remaining.")
    reason = None
    if observable:
        if bid >= .95:
            reason = "absolute 95¢ target"
        elif bid >= target:
            reason = "profit target"
        elif stop_enabled and stop_hit:
            reason = "stop loss"
        elif expiry_enabled and expiry_hit:
            reason = "before-expiry rule"
        elif hold_enabled and hold_hit:
            reason = "maximum hold time"
        elif reversal_enabled and reversal_hit:
            reason = "momentum reversal"
    floor = .95 if reason == "absolute 95¢ target" else target if reason == "profit target" else None
    fills = engine._sweep(bids, position["contracts"], floor=floor) if reason else []
    quantity = sum(size for _, size in fills)
    executable = bool(reason and revision and quantity)
    add("sale_ready", "Sell execution", "met" if executable or settlement else "building" if reason or closed or not observable else "blocked",
        "Official settlement pays remaining contracts without order-book depth." if settlement else
        f"{reason.capitalize()}: {quantity}/{position['contracts']} contracts executable on this update." if executable else
        f"{reason.capitalize()} triggered; waiting for unused executable depth." if reason else
        "Waiting for official settlement." if closed else "No executable exit trigger on this update.")
    summary = (f"{side.upper()} sell conditions active: {reason}." if executable else
               f"{side.upper()} exit triggered; waiting for fresh unused depth." if reason else
               "Waiting for official settlement." if closed else f"Holding {side.upper()}; watching sell conditions.")
    return {"mode": "exit", "side": side, "summary": summary, "updated_at": wall_time, "items": items}
