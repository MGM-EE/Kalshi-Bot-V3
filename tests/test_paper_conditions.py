"""Condition indicators explain decisions without changing fills or saved state."""
import copy
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from feedboard.paper import PaperConfig, PaperEngine
from tests.test_paper import EPOCH, book, feeds, iso, warm


def items(engine):
    return {item["key"]: item for item in engine.snapshot()["conditions"]["items"]}


def watch(**config_values):
    config = PaperConfig(entries_enabled=False, require_toward_strike=False, **config_values)
    engine, events = warm(PaperEngine(config))
    assert not events
    engine.set_config(replace(config, entries_enabled=True))
    return engine


class PaperConditionTests(unittest.TestCase):
    def test_cold_start_and_conditions_are_not_persisted(self):
        engine = PaperEngine()
        self.assertEqual(engine.snapshot()["conditions"], {
            "mode": "entry", "side": None, "summary": "Waiting for the first market update.",
            "updated_at": None, "items": [],
        })
        self.assertNotIn("conditions", engine.dump_state())
        engine.step({}, {}, now=0, wall_time=EPOCH)
        view = engine.snapshot()["conditions"]
        self.assertEqual(view["updated_at"], EPOCH)
        self.assertEqual(len(view["items"]), 14)
        self.assertEqual(items(engine)["data"]["state"], "building")
        json.dumps(view, allow_nan=False)

    def test_all_entry_checks_use_one_candidate_and_exact_thresholds(self):
        engine = watch(min_momentum_bps=(65080 / 65060 - 1) * 10000,
                       flow_imbalance=.8, max_spread=.02)
        view = engine.snapshot()["conditions"]
        self.assertEqual(view["side"], "up")
        self.assertIn("conditions met", view["summary"])
        self.assertTrue(all(row["state"] in ("met", "disabled") for row in view["items"]))
        self.assertIn("+80.0%", items(engine)["flow"]["detail"])
        self.assertIn("2.0¢", items(engine)["spread"]["detail"])
        self.assertIn("5/5", items(engine)["depth"]["detail"])
        self.assertIn("fees", items(engine)["cash"]["detail"])
        engine.set_config(replace(engine.config, min_momentum_bps=4.))
        self.assertEqual(items(engine)["momentum"]["state"], "building")
        engine.set_config(replace(engine.config, flow_imbalance=.81))
        self.assertEqual(items(engine)["flow"]["state"], "building")

    def test_toward_strike_candidate_does_not_borrow_opposite_side_flow_or_momentum(self):
        engine, events = warm(PaperEngine(), strike_price=64900, strike_source="kalshi.floor_strike")
        self.assertEqual(events, [])
        self.assertEqual(engine.snapshot()["conditions"]["side"], "down")
        rows = items(engine)
        self.assertEqual(rows["toward_strike"]["state"], "met")
        self.assertEqual(rows["momentum"]["state"], "blocked")
        self.assertEqual(rows["flow"]["state"], "blocked")
        self.assertEqual(rows["large_prints"]["state"], "blocked")
        self.assertIn("DOWN ask 52.0¢", rows["buy_price"]["detail"])

    def test_unverified_floor_and_percentage_band_are_waiting_not_green(self):
        engine, events = warm(PaperEngine(PaperConfig(floor_distance_enabled=True)),
                              strike_price=65100, strike_source="inferred")
        self.assertEqual(events, [])
        self.assertEqual(items(engine)["toward_strike"]["state"], "building")
        self.assertEqual(items(engine)["floor_band"]["state"], "building")
        engine.step(feeds(65084, 22), book(22, strike_price=66000, strike_source="kalshi.floor_strike"),
                    now=21, wall_time=EPOCH + 21)
        self.assertEqual(items(engine)["floor_band"]["state"], "blocked")

    def test_entry_data_transport_symbol_quote_and_ticker_guards(self):
        for change in ("stale", "transport", "symbol", "ticker", "quote"):
            with self.subTest(change=change):
                engine = watch()
                data, depth = feeds(65084, 22), book(22)
                if change == "stale":
                    depth["age_ms"] = 3001
                elif change == "transport":
                    data["kraken"]["transport"] = "rest"
                elif change == "symbol":
                    data["kraken"]["data"]["symbol"] = "ETH/USD"
                elif change == "ticker":
                    depth["ticker"] = []
                else:
                    data["kraken"]["data"]["bid"] = float("nan")
                self.assertEqual(engine.step(data, depth, now=21, wall_time=EPOCH + 21), [])
                self.assertNotEqual(items(engine)["data"]["state"], "met")
                json.dumps(engine.snapshot()["conditions"], allow_nan=False)

    def test_actual_depth_cap_fees_and_consumed_revision(self):
        engine, events = warm(PaperEngine(PaperConfig(require_toward_strike=False)),
                              no=[[.5, 2], [.48, 20]])
        self.assertEqual(events, [])
        self.assertEqual(items(engine)["depth"]["state"], "blocked")
        self.assertIn("2/5", items(engine)["depth"]["detail"])
        engine = watch()
        engine.cash = 2.5  # Not enough after per-fill fees.
        self.assertEqual(items(engine)["cash"]["state"], "blocked")
        engine._consume(book(21))
        self.assertEqual(items(engine)["book_revision"]["state"], "building")
        engine.cooldown_until = EPOCH + 30
        self.assertEqual(items(engine)["cooldown"]["state"], "building")

    def test_timing_reports_actual_90_second_rule_without_enforcing_requested_window(self):
        engine = watch()
        engine.set_config(replace(engine.config, entries_enabled=False))
        engine.step(feeds(65084, 22), book(22, close_time=iso(EPOCH + 111)),
                    now=21, wall_time=EPOCH + 21)
        self.assertEqual(items(engine)["market"]["state"], "met")
        self.assertIn("90s left", items(engine)["market"]["detail"])
        self.assertIn("not enforced", items(engine)["market"]["detail"])
        engine.step(feeds(65088, 23), book(23, close_time=iso(EPOCH + 111)),
                    now=22, wall_time=EPOCH + 22)
        self.assertEqual(items(engine)["market"]["state"], "blocked")

    def test_mode_refreshes_after_entry_and_after_complete_exit(self):
        engine, events = warm()
        self.assertEqual(events[0]["type"], "entry")
        self.assertEqual(engine.snapshot()["conditions"]["mode"], "exit")
        self.assertEqual(items(engine)["book_revision"]["state"], "building")
        events = engine.step(feeds(65084, 22), book(22, yes=[[.96, 20]], no=[[.03, 20]]),
                             now=21, wall_time=EPOCH + 21)
        self.assertEqual(events[0]["reason"], "hard_95c")
        view = engine.snapshot()["conditions"]
        self.assertEqual(view["mode"], "entry")
        self.assertEqual(items(engine)["cooldown"]["state"], "building")
        self.assertEqual(items(engine)["book_revision"]["state"], "building")

    def test_position_uses_frozen_rules_until_explicit_exit_override(self):
        config = PaperConfig(require_toward_strike=False, hold_to_settlement=True,
                             dynamic_exit_min=.90, min_exit_price=.85)
        engine, _ = warm(PaperEngine(config))
        target = engine.position["exit_target"]
        engine.set_config(replace(config, hold_to_settlement=False, dynamic_exit_enabled=False,
                                  min_exit_price=.80, stop_loss=.95))
        rows = items(engine)
        for key in ("stop_loss", "reversal", "max_hold", "expiry"):
            self.assertEqual(rows[key]["state"], "disabled")
        self.assertEqual(rows["profit_target"]["label"], "Dynamic profit target")
        self.assertIn(f"{target * 100:.1f}¢", rows["profit_target"]["detail"])
        engine.apply_exit_controls_to_position(ticker=engine.position["ticker"],
                                                opened_at=engine.position["opened_at"], wall_time=EPOCH + 21)
        self.assertNotEqual(items(engine)["stop_loss"]["state"], "disabled")
        self.assertIn("≤38.0¢", items(engine)["stop_loss"]["detail"])
        self.assertEqual(engine.position["exit_target"], target)

    def test_disabled_exit_switches_and_stale_or_wrong_book_never_claim_ready_sale(self):
        config = PaperConfig(require_toward_strike=False, exit_on_stop_loss=False,
                             exit_on_reversal=False, exit_on_max_hold=False, exit_before_expiry=False)
        engine, _ = warm(PaperEngine(config))
        for change in ("stale", "wrong", "empty"):
            depth = book(22, yes=[[.96, 20]], no=[[.03, 20]])
            if change == "stale":
                depth["age_ms"] = 3001
            elif change == "wrong":
                depth["ticker"] = "KXBTC15M-OTHER"
            else:
                depth["yes"] = []
            self.assertEqual(engine.step(feeds(65084, 22), depth, now=21, wall_time=EPOCH + 21), [])
            rows = items(engine)
            self.assertNotEqual(rows["sale_ready"]["state"], "met")
            self.assertNotEqual(rows["hard_95c"]["state"], "met")
            for key in ("stop_loss", "reversal", "max_hold", "expiry"):
                self.assertEqual(rows[key]["state"], "disabled")

    def test_partial_profit_exit_has_met_trigger_but_consumed_sale_gate(self):
        engine, _ = warm()
        events = engine.step(feeds(65084, 22), book(22, yes=[[.96, 2]], no=[[.03, 20]]),
                             now=21, wall_time=EPOCH + 21)
        self.assertEqual(events[0]["contracts"], 2)
        self.assertEqual(engine.position["contracts"], 3)
        rows = items(engine)
        self.assertEqual(rows["hard_95c"]["state"], "met")
        self.assertEqual(rows["sale_ready"]["state"], "building")
        self.assertIn("waiting for fresh unused depth", engine.snapshot()["conditions"]["summary"])

    def test_settlement_wait_and_verified_result_transition(self):
        engine, _ = warm(PaperEngine(PaperConfig(require_toward_strike=False, hold_to_settlement=True)))
        engine.step(feeds(65084, 22), book(22), now=21, wall_time=EPOCH + 901)
        self.assertEqual(items(engine)["settlement"]["state"], "building")
        self.assertIn("official settlement", engine.snapshot()["conditions"]["summary"])
        self.assertNotEqual(items(engine)["sale_ready"]["state"], "met")
        events = engine.step(feeds(65088, 23), book(23, settlement_result="yes", market_status="settled"),
                             now=22, wall_time=EPOCH + 902)
        self.assertEqual(events[0]["type"], "settlement")
        self.assertEqual(engine.snapshot()["conditions"]["mode"], "entry")

    def test_reversal_trigger_is_explained_even_if_book_already_consumed(self):
        engine, _ = warm()
        reversed_signal = {"midpoint": 65000, "five": -2., "twenty": -3., "floor5": 1., "floor20": 1.}
        with patch.object(engine, "_observe", return_value=reversed_signal):
            self.assertEqual(engine.step(feeds(65000, 22, -1), book(21), now=21, wall_time=EPOCH + 21), [])
        self.assertEqual(items(engine)["reversal"]["state"], "met")
        self.assertEqual(items(engine)["sale_ready"]["state"], "building")
        self.assertIn("momentum reversal triggered", items(engine)["sale_ready"]["detail"].lower())

    def test_snapshot_reads_never_change_strategy_or_persisted_state(self):
        observed = PaperEngine(PaperConfig(require_toward_strike=False))
        control = PaperEngine(PaperConfig(require_toward_strike=False))
        for second in range(28):
            depth = book(second + 1, yes=[[.96, 2]] if second >= 23 else [[.48, 20]])
            kwargs = {"now": second, "wall_time": EPOCH + second}
            expected = control.step(feeds(65000 + second * 4, second + 1), copy.deepcopy(depth), **kwargs)
            actual = observed.step(feeds(65000 + second * 4, second + 1), copy.deepcopy(depth), **kwargs)
            self.assertEqual(actual, expected)
            before = observed.dump_state()
            view = observed.snapshot()
            if view["conditions"]["items"]:
                view["conditions"]["items"][0]["state"] = "mutated by caller"
            self.assertNotEqual(observed.snapshot()["conditions"]["items"][0]["state"], "mutated by caller")
            self.assertEqual(observed.dump_state(), before)
            self.assertEqual(observed.dump_state(), control.dump_state())
        self.assertNotIn("_condition_context", observed.dump_state())
