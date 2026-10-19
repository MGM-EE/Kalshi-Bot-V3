"""Experimental settlement outlook from observed BTC/USD quotes.

This is an uncalibrated display model, not a trading signal or an estimate of an
executable contract price. It models future log-price changes with recent
realized variance and a small, bounded 20-second momentum projection. The fixed
volatility and spot-versus-settlement-index uncertainty floors are deliberately
conservative assumptions, not parameters fitted to profitable trades.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone


WINDOW_SECONDS = 900
WARMUP_SECONDS = 60
MIN_BUCKETS = 12
LOOKBACK_SECONDS = 300
MAX_GAP_SECONDS = 15
FRESH_SECONDS = 3
VOLATILITY_FLOOR = 0.00005  # 0.5 basis points per square-root second.
INDEX_UNCERTAINTY = 0.00025  # 2.5 bps: spot is not Kalshi's settlement index.
MOMENTUM_SECONDS = 20
HISTORY_SECONDS = 5


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        result = parsed.timestamp() if parsed.tzinfo is not None else None
        return result if result is not None and math.isfinite(result) else None
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None


def _iso(value):
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _midpoint(quote):
    if not isinstance(quote, dict) or quote.get("symbol", "BTC/USD") != "BTC/USD":
        return None
    bid, ask = _number(quote.get("bid")), _number(quote.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return None
    mid = bid + (ask - bid) / 2
    return mid if math.isfinite(mid) and mid > 0 else None


def _fresh(source):
    age = _number(source.get("age_ms"))
    return source.get("status") == "live" and age is not None and 0 <= age <= FRESH_SECONDS * 1000


@dataclass(frozen=True)
class _Quote:
    now: float
    wall_time: float
    midpoint: float


class SettlementOutlook:
    """Observe real WebSocket updates once; serve a bounded, read-stable outlook.

    Input observations are capped at one latest quote per monotonic-second bin.
    A quote gap over 15 seconds restarts volatility warmup. Market changes clear
    only the chart: valid recent spot observations can carry across contracts.
    Snapshot reads do not invent observations or advance the forecast/history.
    The displayed countdown does advance with the caller's current wall clock.
    """

    def __init__(self):
        self._quotes = deque(maxlen=360)
        self._latest: _Quote | None = None
        self._sequence = 0
        self._statistics_bucket = None
        self._statistics_value = None
        self._identity = None
        self._history = deque(maxlen=181)
        self._history_sequence = None

    def observe(self, quote: dict, *, now: float, wall_time: float,
                transport: str = "websocket", symbol: str = "BTC/USD"):
        """Ingest one real normalized Kraken quote; ignore invalid/REST updates."""
        now, wall_time = _number(now), _number(wall_time)
        mid = _midpoint(quote)
        if (transport != "websocket" or symbol != "BTC/USD" or mid is None
                or now is None or now < 0 or wall_time is None or _iso(wall_time) is None):
            return
        previous = self._latest
        if previous is not None:
            # Replayed timestamps and clock discontinuities must not manufacture
            # a long observed history or a misleading remaining-time forecast.
            if now <= previous.now:
                return
            if (wall_time < previous.wall_time
                    or abs((wall_time - previous.wall_time) - (now - previous.now)) > FRESH_SECONDS):
                # A real system-clock correction must recover rather than
                # reject every subsequent quote against the old clock offset.
                # Reanchor to this observation and require a fresh warmup.
                self._quotes.clear()
                self._statistics_bucket = None
                self._history.clear()
                self._history_sequence = None
            if now - previous.now > MAX_GAP_SECONDS:
                self._quotes.clear()
                self._statistics_bucket = None
        observed = _Quote(now, wall_time, mid)
        bucket = math.floor(now)
        if self._quotes and math.floor(self._quotes[-1].now) == bucket:
            self._quotes[-1] = observed
        else:
            self._quotes.append(observed)
        while self._quotes and now - self._quotes[0].now > LOOKBACK_SECONDS:
            self._quotes.popleft()
        self._latest = observed
        self._sequence += 1

    def _statistics(self):
        """Calculate at most once per second; ingestion itself remains O(1)."""
        bucket = math.floor(self._latest.now)
        if self._statistics_bucket == bucket:
            return self._statistics_value
        self._statistics_bucket = bucket
        self._statistics_value = None
        quotes = list(self._quotes)
        if len(quotes) < MIN_BUCKETS or quotes[-1].now - quotes[0].now < WARMUP_SECONDS:
            return None
        elapsed = quotes[-1].now - quotes[0].now
        squared_returns = 0.0
        for previous, current in zip(quotes, quotes[1:]):
            gap = current.now - previous.now
            if gap <= 0 or gap > MAX_GAP_SECONDS:
                return None
            change = math.log(current.midpoint) - math.log(previous.midpoint)
            squared_returns += change * change
        variance = max(VOLATILITY_FLOOR ** 2, squared_returns / elapsed)
        old = quotes[-1]
        for observed in reversed(quotes[:-1]):
            old = observed
            if quotes[-1].now - observed.now >= MOMENTUM_SECONDS:
                break
        trend = ((math.log(quotes[-1].midpoint) - math.log(old.midpoint))
                 / (quotes[-1].now - old.now))
        self._statistics_value = variance, trend
        return self._statistics_value

    def _record(self, elapsed, percent):
        if self._history_sequence == self._sequence:
            return
        self._history_sequence = self._sequence
        bucket = math.floor(elapsed / HISTORY_SECONDS)
        point = (bucket, round(elapsed, 3), percent)
        if self._history and self._history[-1][0] == bucket:
            self._history[-1] = point
        elif not self._history or bucket > self._history[-1][0]:
            self._history.append(point)

    def snapshot(self, book: dict, kraken: dict, *, now: float, wall_time: float,
                 enabled: bool = True) -> dict:
        """Return percentages only for fresh, verified, supported market inputs.

        Forecast time is the latest observed quote's time, so extra UI/API reads
        cannot create forecast points. Only an official finalized/settled Kalshi
        result can display 0% or 100%; spot at expiry is never treated as a result.
        """
        book = book if isinstance(book, dict) else {}
        kraken = kraken if isinstance(kraken, dict) else {}
        now, wall_time = _number(now), _number(wall_time)
        ticker = book.get("ticker") if isinstance(book.get("ticker"), str) else None
        strike = _number(book.get("strike_price"))
        if strike is None or strike <= 0 or book.get("strike_source") != "kalshi.floor_strike":
            strike = None
        close = _timestamp(book.get("close_time"))
        opening = close - WINDOW_SECONDS if close is not None else None
        identity = (ticker, strike, close)
        if identity != self._identity:
            self._identity = identity
            self._history.clear()
            self._history_sequence = None
        data = kraken.get("data")
        data = data if isinstance(data, dict) else {}
        mid = _midpoint(data)
        result = {
            "enabled": enabled is True, "status": "unavailable", "ticker": ticker,
            "up_percent": None, "down_percent": None, "favored": None,
            "elapsed_seconds": None, "remaining_seconds": None,
            "open_time": _iso(opening) if opening is not None else None,
            "close_time": _iso(close) if close is not None else None,
            "updated_at": None, "strike_price": strike, "btc_midpoint": mid,
            "message": "Waiting for a verified Bitcoin 15-minute contract.",
            "history": [],
        }

        def finish(status, message):
            result.update(status=status, message=message)
            result["history"] = [{"elapsed_seconds": point[1], "up_percent": point[2]}
                                 for point in self._history]
            return result

        if enabled is not True:
            return finish("unavailable", "Settlement outlook is disabled.")
        if now is None or now < 0 or wall_time is None or _iso(wall_time) is None:
            return finish("unavailable", "Waiting for a valid clock.")
        if (ticker is None or not ticker.startswith("KXBTC15M-") or len(ticker) <= 9
                or book.get("series", "KXBTC15M") != "KXBTC15M"
                or strike is None or opening is None or result["open_time"] is None
                or result["close_time"] is None):
            return finish("unavailable", "Waiting for a verified Kalshi BTC floor and close time.")
        result.update(elapsed_seconds=round(min(WINDOW_SECONDS, max(0, wall_time - opening)), 3),
                      remaining_seconds=round(min(WINDOW_SECONDS, max(0, close - wall_time)), 3))
        if wall_time < opening:
            return finish("unavailable", "This contract's 15-minute window has not started.")
        if not _fresh(book):
            return finish("stale", "Waiting for fresh verified Kalshi market data.")
        market_status = book.get("market_status")
        settlement = book.get("settlement_result")
        if market_status in ("settled", "finalized") and settlement in ("yes", "no"):
            up = 100 if settlement == "yes" else 0
            result.update(up_percent=up, down_percent=100 - up,
                          favored="up" if up else "down")
            return finish("settled", "Official Kalshi settlement: " + ("Up." if up else "Down."))
        if wall_time >= close or market_status in ("closed", "settled", "finalized"):
            return finish("awaiting_settlement", "Awaiting the official Kalshi settlement; spot is not a result.")
        if market_status not in ("open", "active"):
            return finish("unavailable", "Waiting for an active Kalshi contract.")
        if (not _fresh(kraken) or kraken.get("transport") != "websocket"):
            return finish("stale", "Live Kraken BTC/USD WebSocket data is required.")
        if mid is None or data.get("symbol") != "BTC/USD":
            return finish("unavailable", "Waiting for a valid BTC/USD bid and ask.")
        observed = self._latest
        if observed is None:
            return finish("warming_up", "Observing live BTC quotes for at least 60 seconds.")
        age = now - observed.now
        if age < 0 or abs((wall_time - observed.wall_time) - age) > FRESH_SECONDS:
            return finish("unavailable", "Clock changed; waiting for synchronized live observations.")
        if age > FRESH_SECONDS:
            return finish("stale", "The observed BTC quote is stale.")
        if not math.isclose(mid, observed.midpoint, rel_tol=1e-12):
            return finish("unavailable", "Waiting for the current BTC quote to be observed.")
        if observed.wall_time < opening or observed.wall_time >= close:
            return finish("unavailable", "Waiting for a live quote within this contract's window.")
        statistics = self._statistics()
        if statistics is None:
            return finish("warming_up", "Observing live BTC quotes for at least 60 seconds.")
        variance, slope = statistics
        horizon = close - observed.wall_time
        short_horizon = min(horizon, MOMENTUM_SECONDS)
        trend_limit = 0.5 * math.sqrt(variance * short_horizon)
        trend = max(-trend_limit, min(trend_limit, slope * short_horizon))
        uncertainty = math.sqrt(variance * horizon + INDEX_UNCERTAINTY ** 2)
        distance = math.log(mid) - math.log(strike)
        z_score = (distance + trend) / uncertainty
        probability = 0.5 * (1 + math.erf(z_score / math.sqrt(2)))
        up = max(5, min(95, round(probability * 100)))
        result.update(up_percent=up, down_percent=100 - up,
                      favored="up" if up > 50 else "down" if up < 50 else None,
                      updated_at=observed.wall_time)
        self._record(observed.wall_time - opening, up)
        return finish("live", "Experimental estimate from spot distance, recent volatility, and time remaining; uncalibrated.")
