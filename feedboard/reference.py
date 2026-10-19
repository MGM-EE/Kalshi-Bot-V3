"""Authoritative strike metadata for the supported Bitcoin Up/Down contracts."""

from __future__ import annotations

import math
from typing import Any


def normalize_reference(market: dict[str, Any]) -> dict[str, float | str | None]:
    """Keep only a documented numeric strike with unambiguous YES-above semantics.

    KXBTC15M's ``floor_strike`` is the exchange's opening reference value.  It
    represents CF Benchmarks' reference average, not a sampled Kraken price.
    Missing metadata must keep the toward-strike entry filter waiting; titles,
    subtitles, custom_strike objects, and spot quotes are not substitutes.
    """
    missing = {"strike_price": None, "strike_source": None}
    ticker = market.get("ticker")
    if (
        not isinstance(ticker, str)
        or not ticker.startswith("KXBTC15M-")
        or market.get("market_type") != "binary"
        or market.get("strike_type") not in ("greater", "greater_or_equal")
        or market.get("cap_strike") is not None
    ):
        return missing
    value = market.get("floor_strike")
    if value is None or isinstance(value, bool):
        return missing
    try:
        strike = float(value)
    except (TypeError, ValueError, OverflowError):
        return missing
    if not math.isfinite(strike) or strike <= 0:
        return missing
    return {"strike_price": strike, "strike_source": "kalshi.floor_strike"}
