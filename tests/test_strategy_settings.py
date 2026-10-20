"""Validate dashboard settings independently of transport and live feeds."""
import unittest
from dataclasses import replace

from feedboard.paper import PaperConfig
from feedboard.strategy_settings import apply_public_settings, public_settings


class StrategySettingsTests(unittest.TestCase):
    def test_public_settings_round_trip_and_exact_exposed_keys(self):
        config = PaperConfig()
        public = public_settings(config)
        self.assertEqual(set(public), {
            "min_buy_cents", "max_buy_cents", "min_sell_cents", "dynamic_min_cents",
            "dynamic_max_cents", "dynamic_enabled", "toward_strike_only", "entries_enabled",
            "contracts", "stop_loss_cents", "max_spread_cents", "max_hold_seconds",
            "cooldown_seconds", "min_momentum_bps", "min_flow_percent",
            "hold_to_settlement", "exit_on_stop_loss", "exit_on_reversal",
            "exit_on_max_hold", "exit_before_expiry",
            "floor_distance_enabled", "max_above_floor_percent", "max_below_floor_percent",
            "entry_window_enabled", "entry_start_minute", "entry_end_minute", "confirmation_enabled",
            "confirmation_seconds", "choppiness_enabled", "min_trend_efficiency_percent",
            "range_bias_enabled", "range_bias_percent",
        })
        self.assertEqual(apply_public_settings(config, public), config)
        self.assertTrue(public["toward_strike_only"])

    def test_partial_update_preserves_private_configuration_and_original(self):
        config = PaperConfig(initial_cash=4321, warmup_seconds=35, contracts=7)
        updated = apply_public_settings(config, {"min_buy_cents": 25})
        self.assertEqual(updated, replace(config, min_entry_price=0.25))
        self.assertEqual(config.min_entry_price, 0.10)
        self.assertEqual((updated.initial_cash, updated.warmup_seconds, updated.contracts),
                         (4321, 35, 7))

    def test_units_are_converted_in_both_directions(self):
        patch = {"min_buy_cents": 15, "max_buy_cents": 65, "min_sell_cents": 75,
                 "dynamic_min_cents": 77, "dynamic_max_cents": 90, "stop_loss_cents": 8.5,
                 "max_spread_cents": 3.5, "min_flow_percent": 25, "min_momentum_bps": 1.25,
                 "max_hold_seconds": 200, "cooldown_seconds": 40, "contracts": 10}
        config = apply_public_settings(PaperConfig(), patch)
        self.assertEqual((config.min_entry_price, config.max_entry_price, config.min_exit_price),
                         (0.15, 0.65, 0.75))
        self.assertEqual((config.dynamic_exit_min, config.dynamic_exit_max), (0.77, 0.90))
        self.assertEqual((config.stop_loss, config.max_spread, config.flow_imbalance),
                         (0.085, 0.035, 0.25))
        for key, value in patch.items():
            self.assertEqual(public_settings(config)[key], value)

    def test_public_cents_and_percent_round_float_noise(self):
        config = PaperConfig(stop_loss=0.29000000000000004, flow_imbalance=0.29000000000000004)
        self.assertEqual(public_settings(config)["stop_loss_cents"], 29)
        self.assertEqual(public_settings(config)["min_flow_percent"], 29)

    def test_boolean_toggles_accept_only_booleans(self):
        keys = ("dynamic_enabled", "toward_strike_only", "entries_enabled",
                "hold_to_settlement", "exit_on_stop_loss", "exit_on_reversal",
                "exit_on_max_hold", "exit_before_expiry", "floor_distance_enabled",
                "entry_window_enabled", "confirmation_enabled", "choppiness_enabled", "range_bias_enabled")
        for key in keys:
            with self.subTest(key=key):
                config = apply_public_settings(PaperConfig(), {key: False})
                self.assertFalse(public_settings(config)[key])
                self.assertTrue(public_settings(apply_public_settings(config, {key: True}))[key])
            for invalid in (0, 1, "true", "false", None, [], {}):
                with self.subTest(key=key, invalid=invalid):
                    with self.assertRaisesRegex(ValueError, "true or false"):
                        apply_public_settings(PaperConfig(), {key: invalid})

    def test_numeric_settings_reject_strings_booleans_nonfinite_and_overflow(self):
        keys = set(public_settings(PaperConfig())) - {
            "floor_distance_enabled",
            "entry_window_enabled", "confirmation_enabled", "choppiness_enabled", "range_bias_enabled",
            "dynamic_enabled", "toward_strike_only", "entries_enabled",
            "hold_to_settlement", "exit_on_stop_loss", "exit_on_reversal",
            "exit_on_max_hold", "exit_before_expiry",
        }
        invalids = ("10", True, False, None, [], {}, float("nan"), float("inf"),
                    -float("inf"), 10**1000)
        for key in keys:
            for invalid in invalids:
                with self.subTest(key=key, invalid=repr(invalid)[:30]):
                    with self.assertRaisesRegex(ValueError, "finite number"):
                        apply_public_settings(PaperConfig(), {key: invalid})

    def test_contracts_are_strict_whole_numbers(self):
        for value in (1.0, 5.5, 0, 101):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    apply_public_settings(PaperConfig(), {"contracts": value})
        for value in (1, 100):
            self.assertEqual(apply_public_settings(PaperConfig(), {"contracts": value}).contracts, value)

    def test_unknown_and_private_fields_are_rejected(self):
        for key in ("initial_cash", "series", "symbol", "warmup_seconds", "min_entry_price", "oops", 5):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "Unknown strategy settings"):
                    apply_public_settings(PaperConfig(), {key: 1})

    def test_empty_or_non_object_updates_are_rejected(self):
        for value in ({}, [], None, "", 1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "at least one"):
                    apply_public_settings(PaperConfig(), value)

    def test_range_limits_are_enforced(self):
        bounds = {
            "min_buy_cents": (0.99, 95.01), "max_buy_cents": (0.99, 95.01),
            "min_sell_cents": (0.99, 95.01), "dynamic_min_cents": (0.99, 95.01),
            "dynamic_max_cents": (0.99, 95.01), "stop_loss_cents": (0.09, 95.01),
            "max_spread_cents": (-0.01, 20.01), "max_hold_seconds": (0.99, 3600.01),
            "cooldown_seconds": (-0.01, 3600.01), "min_momentum_bps": (0.009, 100.01),
            "entry_start_minute": (-.1, 15.1), "entry_end_minute": (-.1, 15.1),
            "confirmation_seconds": (.09, 30.1), "min_trend_efficiency_percent": (-.1, 100.1), "range_bias_percent": (-.1, 25.1),
            "min_flow_percent": (-0.01, 100.01),
        }
        for key, invalids in bounds.items():
            for value in invalids:
                with self.subTest(key=key, value=value):
                    with self.assertRaisesRegex(ValueError, "between"):
                        apply_public_settings(PaperConfig(), {key: value})

    def test_custom_ranges_and_boundary_settings_are_valid(self):
        config = apply_public_settings(PaperConfig(), {
            "min_buy_cents": 1, "max_buy_cents": 90, "min_sell_cents": 95,
            "dynamic_min_cents": 95, "dynamic_max_cents": 95, "contracts": 100,
            "stop_loss_cents": 0.1, "max_spread_cents": 0, "max_hold_seconds": 1,
            "cooldown_seconds": 0, "min_momentum_bps": 0.01, "min_flow_percent": 0,
        })
        self.assertEqual(config.min_exit_price, 0.95)
        self.assertEqual(config.dynamic_exit_max, 0.95)
        config = apply_public_settings(config, {
            "stop_loss_cents": 95, "max_spread_cents": 20, "max_hold_seconds": 3600,
            "cooldown_seconds": 3600, "min_momentum_bps": 100, "min_flow_percent": 100,
        })
        self.assertEqual(config.flow_imbalance, 1)

    def test_buy_and_sell_cross_field_constraints_are_atomic(self):
        cases = [({"min_buy_cents": 76}, "Minimum buy"),
                 ({"max_buy_cents": 80}, "Minimum sell"),
                 ({"min_sell_cents": 75}, "Minimum sell"),
                 ({"dynamic_min_cents": 94, "dynamic_max_cents": 90}, "Dynamic sell minimum"),
                 ({"dynamic_min_cents": 79}, "below minimum sell")]
        config = PaperConfig()
        for patch, message in cases:
            with self.subTest(patch=patch):
                with self.assertRaisesRegex(ValueError, message):
                    apply_public_settings(config, patch)
                self.assertEqual(config, PaperConfig())

    def test_disabled_dynamic_range_can_be_below_minimum_sell_until_reenabled(self):
        config = apply_public_settings(PaperConfig(), {
            "dynamic_enabled": False, "dynamic_min_cents": 50, "dynamic_max_cents": 70,
        })
        self.assertFalse(config.dynamic_exit_enabled)
        with self.assertRaisesRegex(ValueError, "below minimum sell"):
            apply_public_settings(config, {"dynamic_enabled": True})
        self.assertTrue(apply_public_settings(config, {
            "dynamic_enabled": True, "dynamic_min_cents": 80, "dynamic_max_cents": 90,
        }).dynamic_exit_enabled)

    def test_hard_95_cent_exit_limit_cannot_be_removed(self):
        for key in ("min_sell_cents", "dynamic_min_cents", "dynamic_max_cents"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "95 cents"):
                    apply_public_settings(PaperConfig(), {key: 96, "dynamic_enabled": False})


if __name__ == "__main__":
    unittest.main()
