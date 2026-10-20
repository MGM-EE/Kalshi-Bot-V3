"""Compact causal decision inputs, with explicit delta bases and resync anchors.

This records the paper engine's observations, not raw exchange events. Attempted
event IDs are never evidence of a durable fill; the journal and paper_fill are.
"""
from __future__ import annotations

import copy
import math
from dataclasses import asdict

FORMAT = "paper-decision-delta-v1"
ANCHOR_SECONDS = .75
BOOK_LEVEL_LIMIT = 100


def finite_copy(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): finite_copy(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [finite_copy(item) for item in value]
    return value


def pick(value, keys):
    return {key: finite_copy(value.get(key)) for key in keys}


def decision_frame(engine, events):
    """Copy bounded numerical fields without rendering snapshots/conditions."""
    context = engine._condition_context or {}
    feeds = context.get("feeds", {})
    kraken, kalshi = feeds.get("kraken", {}), feeds.get("kalshi", {})
    quote = kraken.get("data", {})
    source_keys = ("status", "transport", "age_ms", "updates", "received_at", "request_ms")
    book = context.get("book", {})
    market_keys = ("ticker", "open_time", "close_time", "market_status", "settlement_result",
                   "strike_price", "strike_source", "yes_bid", "yes_ask", "no_bid", "no_ask", "last")
    return {
        "now": context.get("now"), "wall_time": context.get("wall_time"),
        "feeds": {
            "kraken": {**pick(kraken, source_keys), "data": {
                **pick(quote, ("symbol", "bid", "ask", "last", "bid_qty", "ask_qty", "exchange_time")),
                "flow": pick(quote.get("flow") or {}, ("buy_usd", "sell_usd", "large_buy_usd",
                            "large_sell_usd", "count", "age_ms", "window_seconds"))}},
            "kalshi": {**pick(kalshi, source_keys), "data": {
                "series": kalshi.get("data", {}).get("series"),
                "markets": [pick(market, market_keys) for market in
                            kalshi.get("data", {}).get("markets", [])[:5]]}},
        },
        "book": {**pick(book, ("ticker", "status", "age_ms", "version", "open_time", "close_time",
                                "market_status", "settlement_result", "strike_price", "strike_source")),
                 "yes": finite_copy(book.get("yes", [])[:BOOK_LEVEL_LIMIT]),
                 "no": finite_copy(book.get("no", [])[:BOOK_LEVEL_LIMIT])},
        "input_coverage": {"book_level_limit_per_side": BOOK_LEVEL_LIMIT,
                           "book_truncated": any(len(book.get(side, [])) > BOOK_LEVEL_LIMIT
                                                 for side in ("yes", "no")),
                           "kalshi_markets_truncated": len(kalshi.get("data", {}).get("markets", [])) > 5,
                           "raw_exchange_ticks": False},
        "signal": finite_copy(context.get("signal")), "flow": finite_copy(context.get("flow")),
        "entry_quality": finite_copy(context.get("entry_quality")),
        "range_context": finite_copy(context.get("range_context")),
        "entry_requirements": finite_copy(context.get("entry_requirements")),
        "decision_phase": context.get("decision_phase", "exit" if engine.position else "entry"),
        "config_revision": engine.config_revision, "config": asdict(engine.config),
        "status_after_step": engine._status,
        "attempted_event_ids": [event["id"] for event in events],
        "attempted_events_are_committed": False,
    }


def difference(previous, current, path=()):
    changed, removed = {}, []
    for key in previous.keys() - current.keys():
        removed.append([*path, key])
    for key, value in current.items():
        old = previous.get(key)
        if key not in previous:
            changed[key] = value
        elif isinstance(old, dict) and isinstance(value, dict):
            nested, deletions = difference(old, value, (*path, key))
            if nested:
                changed[key] = nested
            removed.extend(deletions)
        elif old != value:
            changed[key] = value
    return changed, removed


def merge(destination, changes):
    for key, value in changes.items():
        if isinstance(value, dict):
            if not isinstance(destination.get(key), dict):
                destination[key] = {}
            merge(destination[key], value)
        else:
            destination[key] = copy.deepcopy(value)


class DecisionEncoder:
    def __init__(self):
        self.sequence = self.bootstrap_id = 0
        self.previous = self.last_anchor = self.previous_sequence = None

    def begin(self):
        self.bootstrap_id += 1
        self.invalidate()
        return self.bootstrap_id

    def invalidate(self):
        self.previous = self.last_anchor = self.previous_sequence = None

    def encode(self, frame):
        self.sequence += 1
        frame = finite_copy(frame)
        now = frame["now"]
        full = (self.previous is None or self.last_anchor is None or now is None
                or now < self.last_anchor or now - self.last_anchor >= ANCHOR_SECONDS)
        payload = {"format": FORMAT, "trace_sequence": self.sequence,
                   "previous_trace_sequence": self.sequence - 1,
                   "bootstrap_id": self.bootstrap_id,
                   "encoding": "full" if full else "delta",
                   "base_trace_sequence": None if full else self.previous_sequence}
        if full:
            payload["frame"] = frame
            self.last_anchor = now
        else:
            payload["changes"], payload["removed"] = difference(self.previous, frame)
        self.previous, self.previous_sequence = frame, self.sequence
        return payload


class DecisionDecoder:
    """Reject broken delta chains, then recover at the next standalone anchor."""
    def __init__(self):
        self.frame = self.sequence = None
        self.gap = False

    def decode(self, payload):
        if payload.get("format") != FORMAT:
            raise ValueError("Unsupported decision trace format")
        sequence = payload["trace_sequence"]
        self.gap = self.sequence is not None and sequence != self.sequence + 1
        if payload.get("encoding") == "full":
            self.frame = copy.deepcopy(payload["frame"])
        elif (payload.get("encoding") == "delta" and self.frame is not None
              and payload.get("base_trace_sequence") == self.sequence and not self.gap):
            for path in payload.get("removed", []):
                target = self.frame
                for key in path[:-1]:
                    target = target[key]
                target.pop(path[-1], None)
            merge(self.frame, payload["changes"])
        else:
            self.frame = None
            self.sequence = sequence
            raise ValueError("Missing decision trace base; wait for a full anchor")
        self.sequence = sequence
        return copy.deepcopy(self.frame)
