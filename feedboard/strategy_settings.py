"""Translate the small dashboard settings form into validated paper rules."""
from __future__ import annotations

import math
from dataclasses import replace

from feedboard.paper import PaperConfig


_NUMBERS = {
    "min_buy_cents": ("min_entry_price", 100, "Minimum buy", 1, 95),
    "max_buy_cents": ("max_entry_price", 100, "Maximum buy", 1, 95),
    "min_sell_cents": ("min_exit_price", 100, "Minimum sell", 1, 95),
    "dynamic_min_cents": ("dynamic_exit_min", 100, "Dynamic sell minimum", 1, 95),
    "dynamic_max_cents": ("dynamic_exit_max", 100, "Dynamic sell maximum", 1, 95),
    "contracts": ("contracts", 1, "Contracts", 1, 100),
    "stop_loss_cents": ("stop_loss", 100, "Stop loss", 0.1, 95),
    "max_spread_cents": ("max_spread", 100, "Maximum spread", 0, 20),
    "max_hold_seconds": ("max_hold_seconds", 1, "Maximum hold seconds", 1, 3600),
    "cooldown_seconds": ("cooldown_seconds", 1, "Cooldown seconds", 0, 3600),
    "min_momentum_bps": ("min_momentum_bps", 1, "Minimum momentum", 0.01, 100),
    "min_flow_percent": ("flow_imbalance", 100, "Minimum aligned flow", 0, 100),
    "max_above_floor_percent": ("max_above_floor_percent", 1, "Maximum above floor percent", 0, 100),
    "max_below_floor_percent": ("max_below_floor_percent", 1, "Maximum below floor percent", 0, 100),
    "entry_start_minute": ("entry_start_minute", 1, "Entry start minute", 0, 15),
    "entry_end_minute": ("entry_end_minute", 1, "Entry end minute", 0, 15),
    "confirmation_seconds": ("confirmation_seconds", 1, "Signal confirmation seconds", .1, 30),
    "min_trend_efficiency_percent": ("min_trend_efficiency", 100, "Minimum trend efficiency percent", 0, 100),
    "range_bias_percent": ("range_bias_strength", 100, "Range bias strength percent", 0, 25),
}
_BOOLEANS = {
    "dynamic_enabled": "dynamic_exit_enabled",
    "toward_strike_only": "require_toward_strike",
    "entries_enabled": "entries_enabled",
    "hold_to_settlement": "hold_to_settlement",
    "exit_on_stop_loss": "exit_on_stop_loss",
    "exit_on_reversal": "exit_on_reversal",
    "exit_on_max_hold": "exit_on_max_hold",
    "exit_before_expiry": "exit_before_expiry",
    "floor_distance_enabled": "floor_distance_enabled",
    "entry_window_enabled": "entry_window_enabled",
    "confirmation_enabled": "confirmation_enabled",
    "choppiness_enabled": "choppiness_enabled",
    "range_bias_enabled": "range_bias_enabled",
}


def public_settings(config: PaperConfig) -> dict:
    """Expose dashboard units without exposing unrelated engine configuration."""
    if not isinstance(config, PaperConfig):
        raise ValueError("Settings require a paper configuration.")
    result = {}
    for key, (attribute, scale, _label, _minimum, _maximum) in _NUMBERS.items():
        value = getattr(config, attribute)
        result[key] = round(value * scale, 6) if scale != 1 else value
    result.update({key: getattr(config, attribute) for key, attribute in _BOOLEANS.items()})
    return result


def apply_public_settings(config: PaperConfig, values: dict) -> PaperConfig:
    """Apply an atomic partial form update, preserving all unexposed rules."""
    current = public_settings(config)
    if not isinstance(values, dict) or not values:
        raise ValueError("Provide at least one strategy setting in an object.")
    unknown = values.keys() - current.keys()
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ValueError(f"Unknown strategy settings: {names}.")

    updates = {}
    for key, value in values.items():
        if key in _BOOLEANS:
            if not isinstance(value, bool):
                raise ValueError(f"{key.replace('_', ' ').capitalize()} must be true or false.")
            updates[_BOOLEANS[key]] = value
            continue
        attribute, scale, label, minimum, maximum = _NUMBERS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{label} must be a finite number.")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError(f"{label} must be a finite number.")
        if key == "contracts" and not isinstance(value, int):
            raise ValueError("Contracts must be a whole number from 1 to 100.")
        if not minimum <= value <= maximum:
            unit = " cents" if key.endswith("_cents") else ""
            raise ValueError(f"{label} must be between {minimum:g} and {maximum:g}{unit}.")
        updates[attribute] = value / scale if scale != 1 else value

    current.update(values)
    if current["min_buy_cents"] > current["max_buy_cents"]:
        raise ValueError("Minimum buy cannot exceed maximum buy.")
    if current["max_buy_cents"] >= current["min_sell_cents"]:
        raise ValueError("Minimum sell must be greater than maximum buy.")
    if current["dynamic_min_cents"] > current["dynamic_max_cents"]:
        raise ValueError("Dynamic sell minimum cannot exceed its maximum.")
    if current["dynamic_enabled"] and current["dynamic_min_cents"] < current["min_sell_cents"]:
        raise ValueError("Dynamic sell minimum cannot be below minimum sell while dynamic exits are enabled.")
    if current["entry_start_minute"] >= current["entry_end_minute"]:
        raise ValueError("Entry start minute must be before entry end minute.")
    return replace(config, **updates)
