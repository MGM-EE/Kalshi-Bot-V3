"""Small, bounded public-market inputs for the paper trader.

Kalshi publishes bids for both outcomes; an ask must be derived from the other
outcome's bids by the caller. Kraken ``side`` is the aggressor/taker's side.
Large prints describe trade size, never an identified trader or "whale".
"""

from __future__ import annotations

import math
import sys
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def normalize_orderbook(payload: Any) -> dict[str, list[list[float]]]:
    """Return at most five actual bid levels per side, highest bid first.

    Prefer current dollar/fixed-point fields over legacy integer-cent bids.
    Invalid individual levels are omitted and missing/null sides stay empty.
    An invalid outer response raises instead of falling back to older fields.
    """
    if not isinstance(payload, dict):
        raise ValueError("Kalshi orderbook response is not an object")
    current = "orderbook_fp" in payload
    book = payload.get("orderbook_fp" if current else "orderbook")
    if not isinstance(book, dict):
        raise ValueError("Kalshi response has no orderbook object")
    result = {}
    for side in ("yes", "no"):
        rows = book.get(f"{side}_dollars" if current else side)
        levels: dict[float, float] = {}
        if rows is not None and not isinstance(rows, list):
            raise ValueError("Kalshi orderbook side is not a levels array")
        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                continue
            price, quantity = _number(row[0]), _number(row[1])
            if price is None or quantity is None or quantity <= 0:
                continue
            if not current:
                if not price.is_integer():
                    continue
                price /= 100
            if not 0 <= price <= 1:
                continue
            # Snapshot levels should be unique. Do not sum duplicate rows and
            # overstate executable depth if a malformed response contains them.
            levels[price] = min(levels.get(price, quantity), quantity)
        result[side] = [[price, levels[price]] for price in sorted(levels, reverse=True)[:5]]
    # YES asks are 1 - NO bids (and vice versa). Crossed outcome bids would
    # create an impossible negative spread and fictional paper-trading profit.
    if result["yes"] and result["no"] and result["yes"][0][0] + result["no"][0][0] > 1 + 1e-9:
        raise ValueError("Kalshi orderbook outcome bids are crossed")
    return result


@dataclass
class _Bucket:
    key: int
    first_at: float
    buy_usd: float = 0.0
    sell_usd: float = 0.0
    large_buy_usd: float = 0.0
    large_sell_usd: float = 0.0
    count: int = 0


class TradeFlow:
    """Aggregate recent Kraken updates in 250 ms buckets, with bounded dedup.

    Buckets expire from their *first* event, so retained data never exceeds the
    twenty-second window (the boundary may omit up to 250 ms of newer trades).
    Initial snapshots never count as fresh flow. ``now`` is monotonic seconds;
    supplying wall_time also rejects old or implausibly future exchange events.
    """

    window_seconds = 20.0
    bucket_seconds = 0.25
    max_buckets = 81
    max_trade_ids = 8192
    large_print_usd = 100_000.0

    def __init__(self):
        self._buckets: dict[int, _Bucket] = {}
        self._trade_ids: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._last_at: float | None = None

    def _expire(self, now: float) -> None:
        for key in [key for key, bucket in self._buckets.items()
                    if now - bucket.first_at >= self.window_seconds]:
            del self._buckets[key]
        # Retain replay protection longer than the signal window, still bounded
        # by both time and count during a burst or a reconnect.
        while self._trade_ids and now - next(iter(self._trade_ids.values())) >= 60:
            self._trade_ids.popitem(last=False)

    @staticmethod
    def _trade_id(value: Any) -> str | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value >= 0 and value.bit_length() <= 107:
            text = str(value)
        elif isinstance(value, str) and value.isascii() and value.isdecimal():
            text = value.lstrip("0") or "0"
        else:
            return None
        return text if len(text) <= 32 else None

    def add_payload(self, payload: Any, symbol: str, now: float,
                    *, wall_time: float | None = None) -> None:
        """Accept WS v2 ``trade/update`` rows only; ignore malformed records."""
        now = _number(now)
        if now is None or (self._last_at is not None and now < self._last_at):
            return
        self._expire(now)
        if not isinstance(payload, dict) or payload.get("channel") != "trade" or payload.get("type") != "update":
            return
        rows = payload.get("data")
        if not isinstance(rows, list):
            return
        wall_time = _number(wall_time)
        for row in rows:
            if not isinstance(row, dict) or row.get("symbol") != symbol or row.get("side") not in ("buy", "sell"):
                continue
            price, quantity = _number(row.get("price")), _number(row.get("qty"))
            trade_id = self._trade_id(row.get("trade_id"))
            if price is None or quantity is None or price <= 0 or quantity <= 0 or trade_id is None:
                continue
            notional = price * quantity
            if not math.isfinite(notional) or notional <= 0:
                continue
            event_at = now
            if wall_time is not None:
                try:
                    stamp = row["timestamp"]
                    if not isinstance(stamp, str):
                        continue
                    when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    if when.tzinfo is None:
                        continue
                    event_age = wall_time - when.timestamp()
                    if not -5 <= event_age < self.window_seconds:
                        continue
                    event_at = now - max(0, event_age)
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
            identifier = (symbol, trade_id)
            if identifier in self._trade_ids:
                continue
            key = math.floor(event_at / self.bucket_seconds)
            if key not in self._buckets:
                self._buckets[key] = _Bucket(key=key, first_at=event_at)
            bucket = self._buckets[key]
            bucket.first_at = min(bucket.first_at, event_at)
            field = f"{row['side']}_usd"
            total = getattr(bucket, field) + notional
            # Keep every aggregate finite, even for absurd but finite inputs.
            if not math.isfinite(total) or total > sys.float_info.max / (self.max_buckets + 1):
                continue
            setattr(bucket, field, total)
            if notional >= self.large_print_usd:
                large_field = f"large_{row['side']}_usd"
                setattr(bucket, large_field, getattr(bucket, large_field) + notional)
            bucket.count += 1
            self._trade_ids[identifier] = now
            while len(self._buckets) > self.max_buckets:
                del self._buckets[min(self._buckets)]
            while len(self._trade_ids) > self.max_trade_ids:
                self._trade_ids.popitem(last=False)
            self._last_at = max(self._last_at, event_at) if self._last_at is not None else event_at

    def snapshot(self, now: float) -> dict:
        now = _number(now)
        if now is None:
            raise ValueError("Trade flow time must be finite")
        self._expire(now)
        sums = {field: sum(getattr(bucket, field) for bucket in self._buckets.values())
                for field in ("buy_usd", "sell_usd", "large_buy_usd", "large_sell_usd", "count")}
        return {**sums, "age_ms": round(max(0, now - self._last_at) * 1000) if self._last_at is not None else None,
                "window_seconds": self.window_seconds}
