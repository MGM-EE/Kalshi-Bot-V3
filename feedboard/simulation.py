"""Fast candle-open price/time experiments, NOT a replay of the live bot.

No high/low touch fills, same-bar exits, future-volume filters, or winner-based
direction selection. Quote size, queue position, BTC momentum, and dynamic exit
signals are unavailable in this historical dataset and are not simulated.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass

from .paper import taker_fee


@dataclass(frozen=True, slots=True)
class SimConfig:
    buy_min_cents: float = 10
    buy_max_cents: float = 40
    sell_target_cents: float | None = 80
    start_minute: int = 5
    end_minute: int = 13
    max_spread_cents: float = 4
    side: str = "both"
    contracts: int = 5
    slippage_cents: float = 1
    stop_loss_cents: float | None = None

    def __post_init__(self):
        numbers = [self.buy_min_cents, self.buy_max_cents, self.max_spread_cents, self.slippage_cents]
        numbers.extend(v for v in (self.sell_target_cents, self.stop_loss_cents) if v is not None)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in numbers):
            raise ValueError("Simulation prices must be finite numbers")
        if (not 1 <= self.buy_min_cents <= self.buy_max_cents <= 95
                or self.sell_target_cents is not None and not self.buy_max_cents < self.sell_target_cents <= 95
                or not 0 <= self.max_spread_cents <= 20 or not 0 <= self.slippage_cents <= 10
                or self.stop_loss_cents is not None and not .1 <= self.stop_loss_cents <= 95):
            raise ValueError("Invalid simulation price/spread/slippage/stop range")
        if (any(isinstance(v, bool) or not isinstance(v, int) for v in
                (self.start_minute, self.end_minute, self.contracts))
                or not 0 <= self.start_minute < self.end_minute <= 15
                or not 1 <= self.contracts <= 100 or self.side not in ("up", "down", "both")):
            raise ValueError("Invalid simulation window, side, or contract count")

    @property
    def config_id(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:12]


@dataclass(slots=True)
class Stats:
    markets: int = 0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl: float = 0
    gross_pnl: float = 0
    fees: float = 0
    positive_pnl: float = 0
    negative_pnl: float = 0
    max_realized_drawdown: float = 0
    target_exits: int = 0
    stop_exits: int = 0
    settlements: int = 0
    cash_blocked_markets: int = 0

    def public(self):
        result = asdict(self)
        result.update(win_rate=self.wins / self.trades if self.trades else None,
                      average_pnl=self.net_pnl / self.trades if self.trades else None,
                      profit_factor=self.positive_pnl / -self.negative_pnl if self.negative_pnl else None)
        return {key: round(value, 6) if isinstance(value, float) else value for key, value in result.items()}


def run_config(config, markets, *, initial_cash=1000., details=False):
    if isinstance(initial_cash, bool) or not isinstance(initial_cash, (int, float)) or not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("Initial simulated cash must be positive and finite")
    cash = peak = initial_cash
    busy_until = 0
    stats, trades = Stats(markets=len(markets)), []
    minimum, maximum = config.buy_min_cents / 100, config.buy_max_cents / 100
    target = config.sell_target_cents / 100 if config.sell_target_cents is not None else None
    slip, spread_limit = config.slippage_cents / 100, config.max_spread_cents / 100
    stop = config.stop_loss_cents / 100 if config.stop_loss_cents is not None else None
    quantity = config.contracts
    for market in markets:
        side = None
        entry = entry_at = entry_fee = exit_price = exit_at = exit_fee = None
        reason = "settlement"
        cash_blocked = False
        start, end = market.close - 900 + config.start_minute * 60, market.close - 900 + config.end_minute * 60
        for bar in market.bars:
            bid, ask = bar.yes_bid, bar.yes_ask
            if bid is None or ask is None or bid > ask + 1e-9:
                continue
            if side is None:
                if not start <= bar.start < end or bar.start < busy_until or ask - bid > spread_limit + 1e-9:
                    continue
                up, down = ask + slip, 1 - bid + slip
                up_ok = config.side != "down" and 0 < ask < 1 and minimum - 1e-9 <= up <= maximum + 1e-9
                down_ok = config.side != "up" and 0 < 1 - bid < 1 and minimum - 1e-9 <= down <= maximum + 1e-9
                if not up_ok and not down_ok:
                    continue
                candidate = "up" if up_ok and (not down_ok or up <= down) else "down"
                price = up if candidate == "up" else down
                fee = taker_fee(quantity, price)
                if quantity * price + fee > cash + 1e-9:
                    cash_blocked = True
                    continue
                side, entry, entry_fee, entry_at = candidate, price, fee, bar.start
                cash -= quantity * entry + fee
                # Future observations only: no entry and exit in one minute.
                continue
            raw_bid = bid if side == "up" else 1 - ask
            if raw_bid <= 0:
                continue
            sale = max(0., raw_bid - slip)
            if target is not None and sale >= target - 1e-9:
                reason = "target"
            elif stop is not None and raw_bid <= entry - stop + 1e-9:
                reason = "stop"
            else:
                continue
            exit_price, exit_at, exit_fee = sale, bar.start, taker_fee(quantity, sale)
            break
        if side is None:
            stats.cash_blocked_markets += int(cash_blocked)
            continue
        if exit_price is None:
            exit_price = float((market.result == "yes") == (side == "up"))
            exit_at, exit_fee = market.settled, 0.
        busy_until = max(busy_until, exit_at)
        cash += quantity * exit_price - exit_fee
        gross = quantity * (exit_price - entry)
        fees = entry_fee + exit_fee
        pnl = gross - fees
        stats.trades += 1
        stats.wins += int(pnl > 1e-9)
        stats.losses += int(pnl < -1e-9)
        stats.gross_pnl += gross
        stats.fees += fees
        stats.positive_pnl += max(0., pnl)
        stats.negative_pnl += min(0., pnl)
        stats.net_pnl = cash - initial_cash
        peak = max(peak, cash)
        stats.max_realized_drawdown = max(stats.max_realized_drawdown, peak - cash)
        stats.target_exits += int(reason == "target")
        stats.stop_exits += int(reason == "stop")
        stats.settlements += int(reason == "settlement")
        if details:
            trades.append({"ticker": market.ticker, "side": side, "contracts": quantity,
                           "entry_ts": entry_at, "exit_ts": exit_at, "close_ts": market.close,
                           "entry_minute": (entry_at - market.close + 900) / 60,
                           "entry_price": round(entry, 6), "exit_price": round(exit_price, 6),
                           "entry_fee": entry_fee, "exit_fee": exit_fee,
                           "pnl": round(pnl, 6), "exit_reason": reason,
                           "official_result": market.result, "cash_after_exit": round(cash, 6)})
    return stats, trades


def chronological_split(markets, fraction=.7):
    if not math.isfinite(fraction) or not 0 < fraction < 1:
        raise ValueError("Train fraction must be between 0 and 1")
    markets = sorted(markets, key=lambda market: (market.close, market.ticker))
    if len(markets) < 2:
        raise ValueError("At least two settled markets are needed for train/test separation")
    split = max(1, min(len(markets) - 1, int(len(markets) * fraction)))
    while split < len(markets) and markets[split].close == markets[split - 1].close:
        split += 1
    if split == len(markets):
        raise ValueError("No independent later market period available for holdout")
    # Settlement information from a training position must be available before
    # the first holdout entry window. Purge boundary markets until its window
    # starts after all training labels are public; no same-round label leakage.
    boundary = max(market.settled for market in markets[:split])
    test_start = split
    while test_start < len(markets) and markets[test_start].close - 900 < boundary:
        test_start += 1
    if test_start == len(markets):
        raise ValueError("No later holdout period remains after settlement-boundary purge")
    return markets[:split], markets[test_start:], markets[split:test_start]


def compare(configs, markets, *, initial_cash=1000., train_fraction=.7, min_train_trades=10):
    if min_train_trades < 1:
        raise ValueError("Minimum training trades must be positive")
    train, test, purged = chronological_split(markets, train_fraction)
    rows = []
    seen = set()
    for config in configs:
        if config.config_id in seen:
            continue
        seen.add(config.config_id)
        training, _ = run_config(config, train, initial_cash=initial_cash)
        testing, _ = run_config(config, test, initial_cash=initial_cash)
        rows.append({"config_id": config.config_id, "config": asdict(config),
                     "train": training.public(), "test": testing.public()})
    if not rows:
        raise ValueError("Simulation grid is empty")
    # Never sort/select by held-out results. CSV keeps this training-only order.
    rows.sort(key=lambda row: (-row["train"]["net_pnl"], row["train"]["max_realized_drawdown"], row["config_id"]))
    selected = next((row for row in rows if row["train"]["trades"] >= min_train_trades), None)
    details = []
    if selected:
        chosen = SimConfig(**selected["config"])
        for name, subset in (("train", train), ("test", test)):
            _, trades = run_config(chosen, subset, initial_cash=initial_cash, details=True)
            details.extend({"split": name, **trade} for trade in trades)
    return rows, selected, details, {"train_markets": len(train), "test_markets": len(test),
        "purged_boundary_tickers": [market.ticker for market in purged],
        "train_fraction": train_fraction, "train_first_close": train[0].close,
        "train_last_close": train[-1].close, "test_first_close": test[0].close,
        "test_last_close": test[-1].close,
        "selection_metric": "training net P&L, then lower realized drawdown; holdout never selects",
        "min_train_trades": min_train_trades}
