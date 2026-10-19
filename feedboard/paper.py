"""Deterministic, public-data-only paper trading. This module cannot place orders.

Prices are dollars per binary contract. All executions sweep observed bid depth;
asks are the complement of opposite-side bids. Signals are heuristics, not a
probability model or a promise of profitable or executable exits.
"""
from __future__ import annotations

import copy
import math
from collections import deque
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from statistics import pstdev


EXIT_CONTROL_FIELDS = (
    "hold_to_settlement", "exit_on_stop_loss", "exit_on_reversal",
    "exit_on_max_hold", "exit_before_expiry",
)
FLOOR_BAND_FIELDS = (
    "floor_distance_enabled", "max_above_floor_percent", "max_below_floor_percent",
)


def _exit_controls(config):
    return {name: getattr(config, name) for name in EXIT_CONTROL_FIELDS}


def _num(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.timestamp() if result.tzinfo is not None else None
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds")


def _fresh(feed, limit=3000):
    age = _num(feed.get("age_ms"))
    return feed.get("status") == "live" and age is not None and 0 <= age <= limit


def _levels(rows):
    """Aggregate validated visible whole-contract depth, sorted by descending bid."""
    result = {}
    if not isinstance(rows, (list, tuple)):
        return []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            continue
        price, size = map(_num, row)
        if price is not None and size is not None and 0 <= price <= 1 and size >= 1:
            price = round(price, 6)
            result[price] = result.get(price, 0) + math.floor(size)
    return sorted(result.items(), reverse=True)


def taker_fee(quantity, price):
    """Estimated Kalshi general taker fee, rounded UP to a cent per fill."""
    return math.ceil(max(0, 0.07 * quantity * price * (1 - price) * 100 - 1e-10)) / 100


@dataclass(frozen=True)
class PaperConfig:
    """Paper rules in dollars per share; sale floors apply to profit exits only.

    Stops, reversals, holding-time limits, expiry risk, and verified settlement
    can realize prices below the configured minimum profit sale price.
    """
    initial_cash: float = 1000.0
    contracts: int = 5
    series: str = "KXBTC15M"
    symbol: str = "BTC/USD"
    warmup_seconds: float = 20.0
    cooldown_seconds: float = 20.0
    max_hold_seconds: float = 180.0
    stop_loss: float = 0.12
    min_entry_price: float = 0.10
    max_entry_price: float = 0.75
    min_exit_price: float = 0.80
    dynamic_exit_min: float = 0.80
    dynamic_exit_max: float = 0.95
    dynamic_exit_enabled: bool = True
    require_toward_strike: bool = True
    entries_enabled: bool = True
    max_spread: float = 0.04
    min_momentum_bps: float = 0.75
    flow_imbalance: float = 0.12
    hold_to_settlement: bool = False
    exit_on_stop_loss: bool = True
    exit_on_reversal: bool = True
    exit_on_max_hold: bool = True
    exit_before_expiry: bool = True
    floor_distance_enabled: bool = False
    max_above_floor_percent: float = 0.10
    max_below_floor_percent: float = 0.10

    def __post_init__(self):
        numeric = (self.initial_cash, self.warmup_seconds, self.cooldown_seconds,
                   self.max_hold_seconds, self.stop_loss, self.min_entry_price,
                   self.max_entry_price, self.min_exit_price, self.dynamic_exit_min,
                   self.dynamic_exit_max, self.max_spread, self.min_momentum_bps,
                   self.flow_imbalance, self.max_above_floor_percent, self.max_below_floor_percent)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or _num(v) is None
               for v in numeric):
            raise ValueError("paper configuration must be finite")
        if any(not isinstance(v, bool) for v in
               (self.dynamic_exit_enabled, self.require_toward_strike, self.entries_enabled,
                self.floor_distance_enabled,
                *(getattr(self, name) for name in EXIT_CONTROL_FIELDS))):
            raise ValueError("paper configuration toggles must be booleans")
        if (isinstance(self.contracts, bool) or not isinstance(self.contracts, int)
                or not 1 <= self.contracts <= 100 or self.initial_cash <= 0
                or self.warmup_seconds < 20 or not 0 <= self.cooldown_seconds <= 3600
                or not 1 <= self.max_hold_seconds <= 3600 or not .001 <= self.stop_loss <= .95
                or not .01 <= self.min_entry_price <= self.max_entry_price < self.min_exit_price <= .95
                or not .01 <= self.dynamic_exit_min <= self.dynamic_exit_max <= .95
                or (self.dynamic_exit_enabled and self.min_exit_price > self.dynamic_exit_min)
                or not 0 <= self.max_spread <= .20
                or not .01 <= self.min_momentum_bps <= 100
                or not 0 <= self.flow_imbalance <= 1
                or not 0 <= self.max_above_floor_percent <= 100
                or not 0 <= self.max_below_floor_percent <= 100):
            raise ValueError("invalid paper configuration")
        if self.series != "KXBTC15M" or self.symbol != "BTC/USD":
            raise ValueError("paper signals currently support KXBTC15M and BTC/USD only")


def _saved_config(value):
    """Accept complete settings from each supported version, never a partial schema."""
    current = {field.name for field in fields(PaperConfig)}
    pre_band = current.difference(FLOOR_BAND_FIELDS)
    legacy = pre_band.difference(EXIT_CONTROL_FIELDS)
    if not isinstance(value, dict) or set(value) not in (current, pre_band, legacy):
        raise ValueError("invalid saved paper configuration")
    return PaperConfig(**value)


class PaperEngine:
    """One small simulated position, bounded state, and no external side effects."""

    def __init__(self, config=None, state=None):
        self.config = config if config is not None else PaperConfig()
        if not isinstance(self.config, PaperConfig):
            raise ValueError("paper configuration must be a PaperConfig")
        self.config_revision = 0
        self.cash = self.config.initial_cash
        self.realized_pnl = 0.0
        self.trade_count = self.wins = self.event_seq = 0
        self.position = None
        self.events = deque(maxlen=20)
        self.cooldown_until = 0.0
        self.last_book_versions = {}
        self.samples = deque(maxlen=4096)
        self._last_update = None
        self._last_observation = None
        self._status = "warming_up"
        self._message = "Warming up BTC momentum (20 seconds)."
        self._mark = None
        # Ephemeral inputs for a read-only condition display; never journaled.
        self._condition_context = None
        if state is not None:
            self._restore(state)

    def _restore(self, state):
        try:
            if not isinstance(state, dict) or state.get("schema") != 1:
                raise ValueError("unsupported state schema")
            if "config" in state:
                self.config = _saved_config(state["config"])
            config_revision = state.get("config_revision", 0)
            if (isinstance(config_revision, bool) or not isinstance(config_revision, int)
                    or config_revision < 0):
                raise ValueError("invalid configuration revision")
            self.config_revision = config_revision
            for key in ("cash", "realized_pnl", "cooldown_until"):
                value = _num(state[key])
                if value is None or (key != "realized_pnl" and value < 0):
                    raise ValueError("invalid monetary or clock state")
                setattr(self, key, value)
            for key in ("trade_count", "wins", "event_seq"):
                value = state[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError("invalid counters")
                setattr(self, key, value)
            if self.wins > self.trade_count:
                raise ValueError("invalid win count")
            versions = state["last_book_versions"]
            if (not isinstance(versions, dict) or len(versions) > 128
                    or any(not isinstance(k, str) or isinstance(v, bool)
                           or not isinstance(v, int) or v < 0 for k, v in versions.items())):
                raise ValueError("invalid consumed revisions")
            self.last_book_versions = dict(versions)
            position = copy.deepcopy(state["position"])
            if position is not None:
                if (not isinstance(position, dict) or position.get("side") not in ("up", "down")
                        or not isinstance(position.get("ticker"), str)
                        or not position["ticker"].startswith(self.config.series + "-")
                        or _timestamp(position.get("close_time")) is None):
                    raise ValueError("invalid position")
                for key in ("contracts", "initial_contracts"):
                    value = position.get(key)
                    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
                        raise ValueError("invalid position size")
                if position["contracts"] > position["initial_contracts"]:
                    raise ValueError("invalid position remainder")
                for key in ("entry_price", "exit_target", "entry_fee_remaining", "opened_at", "realized_pnl"):
                    if _num(position.get(key)) is None:
                        raise ValueError("invalid position amounts")
                if (not 0 < position["entry_price"] < 1 or not .01 <= position["exit_target"] <= .95
                        or position["entry_fee_remaining"] < 0 or position["opened_at"] < 0):
                    raise ValueError("invalid position amounts")
                if "rules" in position:
                    _saved_config(position["rules"])
                if "config_revision" in position:
                    revision = position["config_revision"]
                    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                        raise ValueError("invalid position configuration revision")
                if "exit_controls_revision" in position:
                    revision = position["exit_controls_revision"]
                    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                        raise ValueError("invalid position exit-control revision")
                if "exit_controls_updated_at" in position:
                    if _timestamp(position["exit_controls_updated_at"]) is None:
                        raise ValueError("invalid position exit-control timestamp")
                if "exit_controls_config_revision" in position:
                    revision = position["exit_controls_config_revision"]
                    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                        raise ValueError("invalid position exit-control configuration revision")
            events = state["events"]
            if not isinstance(events, list) or len(events) > 20:
                raise ValueError("invalid event list")
            last_id = 0
            for event in events:
                if (not isinstance(event, dict) or not isinstance(event.get("id"), int)
                        or isinstance(event["id"], bool) or not last_id < event["id"] <= self.event_seq
                        or event.get("type") not in ("entry", "exit", "settlement")
                        or event.get("side") not in ("up", "down")
                        or not isinstance(event.get("ticker"), str)
                        or not isinstance(event.get("reason"), str)
                        or _timestamp(event.get("at")) is None
                        or _num(event.get("price")) is None or not 0 <= event["price"] <= 1
                        or not isinstance(event.get("contracts"), int) or isinstance(event["contracts"], bool)
                        or event["contracts"] <= 0
                        or (event.get("pnl") is not None and _num(event["pnl"]) is None)):
                    raise ValueError("invalid trade event")
                last_id = event["id"]
                if "rules" in event:
                    _saved_config(event["rules"])
                if "config_revision" in event:
                    revision = event["config_revision"]
                    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                        raise ValueError("invalid event configuration revision")
                if "exit_controls" in event:
                    controls = event["exit_controls"]
                    if (not isinstance(controls, dict) or set(controls) != set(EXIT_CONTROL_FIELDS)
                            or any(not isinstance(value, bool) for value in controls.values())):
                        raise ValueError("invalid event exit controls")
                if "exit_controls_revision" in event:
                    revision = event["exit_controls_revision"]
                    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                        raise ValueError("invalid event exit-control revision")
            self.position = position
            self.events = deque(copy.deepcopy(events), maxlen=20)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid saved paper state") from exc

    def dump_state(self):
        return copy.deepcopy({"schema": 1, "config": asdict(self.config),
                              "config_revision": self.config_revision,
                              "cash": self.cash, "realized_pnl": self.realized_pnl,
                              "trade_count": self.trade_count, "wins": self.wins,
                              "event_seq": self.event_seq, "position": self.position,
                              "events": list(self.events), "cooldown_until": self.cooldown_until,
                              "last_book_versions": self.last_book_versions})

    def set_config(self, config):
        """Apply settings to future entries without resetting market history or the account.

        Every open position retains its entry-time rules. Pausing entries never
        prevents profit, risk, or official-settlement exits.
        """
        if not isinstance(config, PaperConfig):
            raise ValueError("paper configuration must be a PaperConfig")
        if config != self.config:
            self.config = config
            self.config_revision += 1

    def _position_config(self):
        if self.position is not None and "rules" in self.position:
            return _saved_config(self.position["rules"])
        # Schema-1 positions created before settings existed retain the original
        # exit rules, even if new account settings differ after an upgrade.
        return PaperConfig(require_toward_strike=False)

    def apply_exit_controls_to_position(self, *, ticker: str, opened_at: float, wall_time: float):
        """Explicitly copy only current exit toggles onto the identified open position.

        Entries retain their original event and other entry-time rules. Identity
        checks prevent a delayed UI action from modifying a newer position.
        """
        if (self.position is None or not isinstance(ticker, str)
                or not isinstance(opened_at, (int, float)) or _num(opened_at) is None
                or self.position["ticker"] != ticker or self.position["opened_at"] != opened_at):
            raise ValueError("The open position changed; refresh before applying exit controls.")
        if not isinstance(wall_time, (int, float)) or _num(wall_time) is None:
            raise ValueError("Exit-control update time must be finite.")
        try:
            updated_at = _iso(wall_time)
        except (ValueError, OverflowError, OSError) as exc:
            raise ValueError("Exit-control update time is outside the supported range.") from exc
        previous = self._position_config()
        if _exit_controls(previous) == _exit_controls(self.config):
            return False
        rules = asdict(previous)
        rules.update(_exit_controls(self.config))
        self.position["rules"] = asdict(PaperConfig(**rules))
        self.position["exit_controls_revision"] = self.position.get("exit_controls_revision", 0) + 1
        self.position["exit_controls_updated_at"] = updated_at
        self.position["exit_controls_config_revision"] = self.config_revision
        return True

    def snapshot(self):
        from .paper_conditions import build_conditions

        position = copy.deepcopy(self.position)
        unrealized = 0.0 if position is None else None
        equity = self.cash if position is None else None
        if position is not None:
            position["mark"] = self._mark
            position["exit_controls"] = _exit_controls(self._position_config())
            position.setdefault("exit_controls_revision", 0)
            if self._mark is not None:
                value = position["contracts"] * self._mark
                fee = taker_fee(position["contracts"], self._mark)
                unrealized = value - fee - position["contracts"] * position["entry_price"] - position["entry_fee_remaining"]
                equity = self.cash + value - fee
        return {"enabled": True, "mode": "paper", "status": self._status,
                "config": asdict(self.config), "config_revision": self.config_revision,
                "message": self._message, "cash": round(self.cash, 4),
                "realized_pnl": round(self.realized_pnl, 4),
                "unrealized_pnl": round(unrealized, 4) if unrealized is not None else None,
                "equity": round(equity, 4) if equity is not None else None,
                "trade_count": self.trade_count, "wins": self.wins, "position": position,
                "last_event": copy.deepcopy(self.events[-1]) if self.events else None,
                "events": copy.deepcopy(list(self.events)),
                "conditions": copy.deepcopy(build_conditions(self, self._condition_context))}

    def _emit(self, kind, position, price, quantity, pnl, reason, wall_time):
        self.event_seq += 1
        event = {"id": self.event_seq, "type": kind, "side": position["side"],
                 "ticker": position["ticker"], "price": round(price, 6), "contracts": quantity,
                 "pnl": round(pnl, 6) if pnl is not None else None,
                 "reason": reason, "at": _iso(wall_time)}
        if kind == "entry":
            event["rules"] = copy.deepcopy(position["rules"])
            event["config_revision"] = position["config_revision"]
        else:
            event["exit_controls"] = _exit_controls(self._position_config())
            event["exit_controls_revision"] = position.get("exit_controls_revision", 0)
        self.events.append(event)
        return copy.deepcopy(event)

    def _consume(self, book):
        self.last_book_versions[book["ticker"]] = book["version"]
        while len(self.last_book_versions) > 128:
            del self.last_book_versions[next(iter(self.last_book_versions))]

    def _available_revision(self, book):
        version = book.get("version")
        return (isinstance(version, int) and not isinstance(version, bool) and version >= 0
                and version > self.last_book_versions.get(book.get("ticker"), -1))

    def _observe(self, kraken, now, config=None):
        config = config or self.config
        data = kraken.get("data", {})
        valid = (_fresh(kraken) and kraken.get("transport") == "websocket"
                 and data.get("symbol") == config.symbol)
        bid, ask = _num(data.get("bid")), _num(data.get("ask"))
        valid = valid and bid is not None and ask is not None and 0 < bid <= ask
        if not valid:
            self.samples.clear()
            self._last_update = None
            self._last_observation = None
            return None
        if self._last_observation is not None and now - self._last_observation > 3:
            self.samples.clear()
            self._last_update = None
        update = (kraken.get("updates"), bid, ask)
        if update != self._last_update:
            self.samples.append((now, (bid + ask) / 2))
            self._last_update = update
            self._last_observation = now
        while self.samples and self.samples[0][0] < now - max(25, config.warmup_seconds + 5):
            self.samples.popleft()
        if len(self.samples) < 5 or now - self.samples[0][0] < config.warmup_seconds:
            return None
        current = self.samples[-1][1]
        old5 = next((p for t, p in reversed(self.samples) if t <= now - 5), None)
        old20 = next((p for t, p in reversed(self.samples) if t <= now - 20), None)
        if old5 is None or old20 is None:
            return None
        increments = [(b[1] / a[1] - 1) * 10000 for a, b in zip(self.samples, list(self.samples)[1:])]
        volatility = pstdev(increments) if len(increments) > 1 else 0
        return {"midpoint": current, "five": (current / old5 - 1) * 10000,
                "twenty": (current / old20 - 1) * 10000,
                "volatility": volatility,
                "floor5": max(config.min_momentum_bps, volatility * .6),
                "floor20": max(config.min_momentum_bps * 4 / 3, volatility * 1.5)}

    def _flow(self, kraken):
        flow = kraken.get("data", {}).get("flow")
        if not isinstance(flow, dict) or not _fresh(kraken):
            return None
        age = _num(flow.get("age_ms"))
        window = _num(flow.get("window_seconds"))
        values = [_num(flow.get(key)) for key in
                  ("buy_usd", "sell_usd", "large_buy_usd", "large_sell_usd", "count")]
        if (age is None or not 0 <= age <= 3000 or window is None or window < 15
                or any(v is None or v < 0 for v in values) or values[4] < 3):
            return None
        buy, sell, large_buy, large_sell, _ = values
        if buy + sell <= 0:
            return None
        large = large_buy + large_sell
        return {"signed": (buy - sell) / (buy + sell),
                "large": (large_buy - large_sell) / large if large else 0.0}

    def _target(self, side, signal, flow, bids, asks, config=None):
        config = config or self.config
        if not config.dynamic_exit_enabled:
            return config.min_exit_price
        direction = 1 if side == "up" else -1
        momentum = max(0., min(1., min(direction * signal["five"] / signal["floor5"],
                                       direction * signal["twenty"] / signal["floor20"]) / 3))
        aggressive = max(0., min(1., direction * flow["signed"]))
        large = max(0., min(1., direction * flow["large"]))
        bid_depth = sum(q for _, q in bids[:3])
        ask_depth = sum(q for _, q in asks[:3])
        liquidity = bid_depth / (bid_depth + ask_depth) if bid_depth + ask_depth else 0
        score = .4 * momentum + .35 * aggressive + .15 * large + .1 * liquidity
        low, high = config.dynamic_exit_min, config.dynamic_exit_max
        return min(.95, high, max(config.min_exit_price, low, round(low + (high - low) * score, 2)))

    @staticmethod
    def _sweep(levels, quantity, floor=None, ceiling=None):
        fills = []
        remaining = quantity
        for price, available in levels:
            if floor is not None and price < floor - 1e-9:
                break
            if ceiling is not None and price > ceiling + 1e-9:
                break
            count = min(remaining, available)
            if count:
                fills.append((price, count))
                remaining -= count
            if remaining == 0:
                break
        return fills

    def _finish(self, wall_time):
        cooldown = self._position_config().cooldown_seconds
        self.trade_count += 1
        self.wins += int(self.position["realized_pnl"] > 0)
        self.position = None
        self._mark = None
        self.cooldown_until = wall_time + cooldown

    def _exit(self, book, levels, reason, wall_time, settlement=None):
        position = self.position
        quantity = position["contracts"]
        if settlement is not None:
            fills = [(float(settlement), quantity)]
        elif not self._available_revision(book):
            return []
        else:
            # Risk exits sweep available depth. Profit exits never fill below the trigger.
            floor = (.95 if reason == "hard_95c" else position["exit_target"]
                     if reason in ("dynamic_target", "fixed_target") else None)
            fills = self._sweep(levels, quantity, floor=floor)
        filled = sum(q for _, q in fills)
        if not filled:
            return []
        proceeds = sum(p * q for p, q in fills)
        fee = sum(taker_fee(q, p) for p, q in fills) if settlement is None else 0
        entry_fee = position["entry_fee_remaining"] * filled / quantity
        pnl = proceeds - fee - position["entry_price"] * filled - entry_fee
        self.cash += proceeds - fee
        self.realized_pnl += pnl
        position["realized_pnl"] += pnl
        position["entry_fee_remaining"] -= entry_fee
        position["contracts"] -= filled
        if settlement is None:
            self._consume(book)
        event = self._emit("exit" if settlement is None else "settlement", position,
                           proceeds / filled, filled, pnl, reason, wall_time)
        if not position["contracts"]:
            self._finish(wall_time)
            self._status, self._message = "watching", "Paper exit complete; cooling down."
        else:
            self._message = "Partial paper exit; waiting for fresh depth for the remainder."
        return [event]

    def step(self, feeds, book, *, now, wall_time):
        """Process a snapshot once. Returned events are newly simulated fills only."""
        if _num(now) is None or _num(wall_time) is None:
            raise ValueError("step clocks must be finite")
        kraken, kalshi = feeds.get("kraken", {}), feeds.get("kalshi", {})
        rules = self._position_config() if self.position else self.config
        signal = self._observe(kraken, now, rules)
        flow = self._flow(kraken)
        self._condition_context = {"feeds": copy.deepcopy(feeds), "book": copy.deepcopy(book),
                                   "signal": signal, "flow": flow, "now": now,
                                   "wall_time": wall_time}
        yes, no = _levels(book.get("yes")), _levels(book.get("no"))
        same_book = self.position is None or book.get("ticker") == self.position["ticker"]
        book_fresh = same_book and _fresh(book)
        book_tradable = book.get("market_status") in ("open", "active")
        side = self.position["side"] if self.position else None
        bids = yes if side == "up" else no
        asks = sorted([(round(1 - p, 6), q) for p, q in (no if side == "up" else yes)])
        self._mark = bids[0][0] if self.position and book_fresh and book_tradable and bids else None
        if self.position:
            self._status, self._message = "holding", "Holding paper " + self.position["side"].upper() + "."
            result = book.get("settlement_result") if same_book else None
            if result in ("yes", "no"):
                payout = int((result == "yes") == (side == "up"))
                return self._exit(book, bids, "verified_settlement", wall_time, settlement=payout)
            close = _timestamp(self.position["close_time"])
            remaining_time = close - wall_time
            if remaining_time <= 0 or (same_book and book.get("market_status") in
                                       ("closed", "settled", "finalized")):
                self._status = "awaiting_settlement"
                self._message = "Market closed; waiting for an official settlement result."
                return []
            if signal and flow and book_fresh and book_tradable and bids and asks:
                self.position["exit_target"] = self._target(side, signal, flow, bids, asks, rules)
            if not book_fresh or not book_tradable or not bids:
                self._message = "Holding; waiting for fresh executable Kalshi depth."
                return []
            bid = bids[0][0]
            direction = 1 if side == "up" else -1
            reason = None
            if bid >= .95:
                reason = "hard_95c"
            elif bid >= self.position["exit_target"]:
                reason = "dynamic_target" if rules.dynamic_exit_enabled else "fixed_target"
            elif (not rules.hold_to_settlement and rules.exit_on_stop_loss
                  and bid <= self.position["entry_price"] - rules.stop_loss):
                reason = "stop_loss"
            elif not rules.hold_to_settlement and rules.exit_before_expiry and remaining_time <= 30:
                reason = "expiry_risk"
            elif (not rules.hold_to_settlement and rules.exit_on_max_hold
                  and wall_time - self.position["opened_at"] >= rules.max_hold_seconds):
                reason = "max_hold"
            elif (not rules.hold_to_settlement and rules.exit_on_reversal
                  and signal and flow and direction * signal["five"] <= -signal["floor5"]
                  and direction * flow["signed"] <= -rules.flow_imbalance):
                reason = "momentum_reversal"
            return self._exit(book, bids, reason, wall_time) if reason else []
        if not self.config.entries_enabled:
            self._status, self._message = "paused", "Paper entries paused; exits remain active."
            return []
        if not (_fresh(kalshi) and _fresh(kraken) and book_fresh):
            self._status, self._message = "paused", "Waiting for fresh market data."
            return []
        if kraken.get("transport") != "websocket" or kraken.get("data", {}).get("symbol") != self.config.symbol:
            self._status, self._message = "paused", "Paper signals require live BTC/USD WebSocket data."
            return []
        ticker = book.get("ticker")
        if not isinstance(ticker, str) or not ticker.startswith(self.config.series + "-"):
            self._status, self._message = "paused", "Paper trading supports Bitcoin 15-minute markets."
            return []
        close = _timestamp(book.get("close_time"))
        if (close is None or close - wall_time < 90 or not book_tradable
                or book.get("settlement_result") in ("yes", "no")):
            self._status, self._message = "watching", "Waiting for an open market with at least 90 seconds remaining."
            return []
        if signal is None:
            self._status, self._message = "warming_up", "Warming up BTC momentum (20 seconds)."
            return []
        strike = _num(book.get("strike_price"))
        if (self.config.require_toward_strike or self.config.floor_distance_enabled) and (strike is None or strike <= 0
                or book.get("strike_source") != "kalshi.floor_strike"):
            self._status, self._message = "watching", "Waiting for verified Kalshi strike price."
            return []
        if self.config.floor_distance_enabled:
            distance = (signal["midpoint"] - strike) / strike * 100
            if (distance < -self.config.max_below_floor_percent - 1e-10
                    or distance > self.config.max_above_floor_percent + 1e-10):
                self._status, self._message = "watching", "BTC is outside the allowed percentage band around the floor."
                return []
        self._status, self._message = "watching", "Watching momentum, trade flow, and visible liquidity."
        if flow is None or wall_time < self.cooldown_until or not self._available_revision(book):
            return []
        for candidate, direction in (("up", 1), ("down", -1)):
            if self.config.require_toward_strike and direction * (strike - signal["midpoint"]) <= 0:
                continue
            if (direction * signal["five"] < signal["floor5"]
                    or direction * signal["twenty"] < signal["floor20"]
                    or direction * flow["signed"] < self.config.flow_imbalance
                    or direction * flow["large"] < -.35):
                continue
            bids = yes if candidate == "up" else no
            asks = sorted([(round(1 - p, 6), q) for p, q in (no if candidate == "up" else yes)])
            if not bids or not asks:
                continue
            ask, bid = asks[0][0], bids[0][0]
            spread = ask - bid
            if (not self.config.min_entry_price <= ask <= self.config.max_entry_price
                    or not -1e-9 <= spread <= self.config.max_spread + 1e-9):
                continue
            fills = self._sweep(asks, self.config.contracts,
                                ceiling=min(ask + .01, self.config.max_entry_price))
            quantity = sum(q for _, q in fills)
            if quantity != self.config.contracts:
                continue
            notional = sum(p * q for p, q in fills)
            fee = sum(taker_fee(q, p) for p, q in fills)
            if notional + fee > self.cash:
                continue
            self.position = {"ticker": ticker, "side": candidate, "contracts": quantity,
                             "initial_contracts": quantity, "entry_price": notional / quantity,
                             "entry_fee_remaining": fee, "opened_at": wall_time,
                             "close_time": book["close_time"], "realized_pnl": 0.,
                             "exit_target": self._target(candidate, signal, flow, bids, asks),
                             "rules": asdict(self.config), "config_revision": self.config_revision}
            self.cash -= notional + fee
            self._consume(book)
            self._mark = bid
            self._status, self._message = "holding", "Paper bought " + candidate.upper() + "."
            return [self._emit("entry", self.position, notional / quantity, quantity, None,
                               "aligned_momentum_flow", wall_time)]
        return []
