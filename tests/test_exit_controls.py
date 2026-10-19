"""Exit policy scenarios: synthetic quotes, no network and no real orders."""
import copy
import json
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timezone
from unittest.mock import patch

from feedboard.paper import EXIT_CONTROL_FIELDS, FLOOR_BAND_FIELDS, PaperConfig, PaperEngine, taker_fee


EPOCH = 1_800_000_000.
TICKER = "KXBTC15M-EXIT-CONTROLS"
EARLY = {"exit_on_stop_loss": "stop_loss", "exit_on_reversal": "momentum_reversal",
         "exit_on_max_hold": "max_hold", "exit_before_expiry": "expiry_risk"}


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def control_values(config):
    return {name: getattr(config, name) for name in EXIT_CONTROL_FIELDS}


def quote(second, direction=1):
    mid = 65000 + direction * second * 4
    return {"kalshi": {"status": "live", "age_ms": 0},
            "kraken": {"status": "live", "age_ms": 0, "updates": second + 1,
                       "transport": "websocket", "data": {
                           "symbol": "BTC/USD", "bid": mid - .5, "ask": mid + .5,
                           "flow": {"buy_usd": 9000 if direction == 1 else 1000,
                                    "sell_usd": 1000 if direction == 1 else 9000,
                                    "large_buy_usd": 2000 if direction == 1 else 0,
                                    "large_sell_usd": 0 if direction == 1 else 2000,
                                    "age_ms": 0, "count": 20, "window_seconds": 20}}}}


def book(version=22, side="up", bid=.48, quantity=20, **extra):
    held, other = ("yes", "no") if side == "up" else ("no", "yes")
    return {"ticker": TICKER, "status": "live", "age_ms": 0, "version": version,
            held: [[bid, quantity]], other: [[max(0, 1 - bid - .02), 20]],
            "market_status": "open", "settlement_result": None,
            "close_time": iso(EPOCH + 900), **extra}


def opened(config=None, side="up"):
    config = config or PaperConfig(require_toward_strike=False)
    engine = PaperEngine(config)
    direction = 1 if side == "up" else -1
    for second in range(21):
        engine.step(quote(second, direction), book(second + 1, side),
                    now=second, wall_time=EPOCH + second)
    if engine.position is None:
        raise AssertionError("synthetic entry fixture failed")
    return engine


def reversed_signal(side):
    direction = -1 if side == "up" else 1
    return {"midpoint": 65000., "five": direction * 3., "twenty": direction * 8.,
            "floor5": 1., "floor20": 1.}


def reversing_step(engine, *, when=875, bid=.35, version=22):
    side = engine.position["side"]
    signed = -.9 if side == "up" else .9
    with patch.object(engine, "_observe", return_value=reversed_signal(side)), \
            patch.object(engine, "_flow", return_value={"signed": signed, "large": signed}):
        return engine.step(quote(21), book(version, side, bid), now=when, wall_time=EPOCH + when)


class ExitPolicyTests(unittest.TestCase):
    def test_defaults_preserve_old_early_exit_policy_and_require_actual_booleans(self):
        config = PaperConfig()
        self.assertEqual(control_values(config), {"hold_to_settlement": False,
                         "exit_on_stop_loss": True, "exit_on_reversal": True,
                         "exit_on_max_hold": True, "exit_before_expiry": True})
        for name in EXIT_CONTROL_FIELDS:
            for value in (0, 1, "false", None, [], 0.0):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    PaperConfig(**{name: value})

    def test_hold_blocks_simultaneous_stop_reversal_time_and_expiry_for_both_sides(self):
        config = PaperConfig(require_toward_strike=False, hold_to_settlement=True)
        for side in ("up", "down"):
            engine = opened(config, side)
            original = engine.dump_state()
            self.assertEqual(reversing_step(engine), [])
            self.assertEqual(engine.position["contracts"], 5)
            self.assertEqual(engine.cash, original["cash"])
            self.assertEqual(engine.realized_pnl, 0)
            self.assertEqual(engine.event_seq, 1)
            self.assertEqual(engine.snapshot()["position"]["exit_controls"], control_values(config))

    def test_each_enabled_exit_reason_fires_and_each_disabled_reason_does_not(self):
        disabled = dict.fromkeys(EARLY, False)
        for side in ("up", "down"):
            for flag, reason in EARLY.items():
                with self.subTest(side=side, reason=reason):
                    config = PaperConfig(require_toward_strike=False, **{**disabled, flag: True})
                    engine = opened(config, side)
                    events = reversing_step(engine)
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]["reason"], reason)
                    self.assertEqual(events[0]["exit_controls"], control_values(config))
                    self.assertEqual(events[0]["exit_controls_revision"], 0)
                    self.assertIsNone(engine.position)
                    self.assertLess(events[0]["pnl"], 0)
                    disabled_engine = opened(replace(config, **{flag: False}), side)
                    self.assertEqual(reversing_step(disabled_engine), [])
                    self.assertEqual(disabled_engine.position["contracts"], 5)

    def test_hold_preserves_dynamic_fixed_profit_and_hard95_exits(self):
        for side in ("up", "down"):
            for dynamic, bid, reason in ((True, .80, "dynamic_target"),
                                         (False, .80, "fixed_target"),
                                         (True, .95, "hard_95c"),
                                         (False, .96, "hard_95c")):
                with self.subTest(side=side, dynamic=dynamic, bid=bid):
                    config = PaperConfig(require_toward_strike=False, hold_to_settlement=True,
                                         dynamic_exit_enabled=dynamic, dynamic_exit_min=.80,
                                         dynamic_exit_max=.80)
                    engine = opened(config, side)
                    events = engine.step({}, book(side=side, bid=bid), now=21, wall_time=EPOCH + 21)
                    self.assertEqual(events[0]["reason"], reason)
                    self.assertEqual(events[0]["price"], bid)
                    self.assertTrue(events[0]["exit_controls"]["hold_to_settlement"])
                    self.assertIsNone(engine.position)

    def test_hold_partial_profit_then_official_settlement_zero_and_one(self):
        for side in ("up", "down"):
            for payout in (0., 1.):
                with self.subTest(side=side, payout=payout):
                    config = PaperConfig(require_toward_strike=False, hold_to_settlement=True)
                    engine = opened(config, side)
                    entry_price = engine.position["entry_price"]
                    entry_fee = engine.position["entry_fee_remaining"]
                    partial = engine.step({}, book(side=side, bid=.95, quantity=2),
                                          now=21, wall_time=EPOCH + 21)[0]
                    self.assertEqual(partial["contracts"], 2)
                    self.assertEqual(engine.position["contracts"], 3)
                    # Expiration and apparently favorable stale prices do not infer an outcome.
                    self.assertEqual(engine.step({}, book(23, side, .99),
                                                 now=901, wall_time=EPOCH + 901), [])
                    self.assertEqual(engine.snapshot()["status"], "awaiting_settlement")
                    result = "yes" if (payout == 1.) == (side == "up") else "no"
                    settled = engine.step({}, book(24, side, .1, market_status="settled",
                                                   settlement_result=result),
                                          now=902, wall_time=EPOCH + 902)[0]
                    self.assertEqual((settled["type"], settled["price"], settled["contracts"]),
                                     ("settlement", payout, 3))
                    expected = 2 * .95 + 3 * payout - 5 * entry_price - entry_fee - taker_fee(2, .95)
                    self.assertAlmostEqual(engine.realized_pnl, expected)
                    self.assertAlmostEqual(engine.cash, 1000 + expected)
                    self.assertTrue(settled["exit_controls"]["hold_to_settlement"])
                    self.assertEqual(engine.trade_count, 1)
                    self.assertIsNone(engine.position)


class ExitControlMigrationTests(unittest.TestCase):
    def legacy_state(self):
        state = opened().dump_state()
        for rules in (state["config"], state["position"]["rules"], state["events"][0]["rules"]):
            for name in (*EXIT_CONTROL_FIELDS, *FLOOR_BAND_FIELDS):
                del rules[name]
        return state

    def test_complete_old_shapes_migrate_account_position_and_original_entry_rules(self):
        original = self.legacy_state()
        restored = PaperEngine(state=json.loads(json.dumps(original)))
        defaults = control_values(PaperConfig())
        self.assertEqual(control_values(restored.config), defaults)
        self.assertEqual(restored.snapshot()["position"]["exit_controls"], defaults)
        self.assertEqual(restored.cash, original["cash"])
        self.assertEqual(restored.events[0], original["events"][0])
        self.assertEqual(restored.event_seq, original["event_seq"])
        # Repeated save/reopen accepts historical pre-control event/position schemas.
        reopened = PaperEngine(state=restored.dump_state())
        self.assertEqual(reopened.dump_state(), restored.dump_state())
        event = reopened.step({}, book(bid=.35), now=21, wall_time=EPOCH + 21)[0]
        self.assertEqual(event["reason"], "stop_loss")
        self.assertEqual(event["exit_controls"], defaults)

    def test_old_no_rules_position_retains_legacy_controls_despite_future_settings(self):
        state = self.legacy_state()
        del state["position"]["rules"]
        restored = PaperEngine(state=state)
        restored.set_config(replace(restored.config, hold_to_settlement=True,
                                    **dict.fromkeys(EARLY, False)))
        self.assertEqual(restored.snapshot()["position"]["exit_controls"], control_values(PaperConfig()))
        event = restored.step({}, book(bid=.35), now=21, wall_time=EPOCH + 21)[0]
        self.assertEqual(event["reason"], "stop_loss")

    def test_unknown_or_arbitrarily_missing_settings_fail_in_all_three_locations(self):
        for shape in ("legacy", "current"):
            for location in ("config", "position", "event"):
                for mutation in ("unknown", "missing_old", "missing_one_new"):
                    if shape == "legacy" and mutation == "missing_one_new":
                        continue
                    state = self.legacy_state() if shape == "legacy" else opened().dump_state()
                    rules = (state["config"] if location == "config" else state["position"]["rules"]
                             if location == "position" else state["events"][0]["rules"])
                    if mutation == "unknown":
                        rules["unknown_exit_setting"] = True
                    elif mutation == "missing_old":
                        del rules["stop_loss"]
                    else:
                        del rules["hold_to_settlement"]
                    with self.subTest(shape=shape, location=location, mutation=mutation), \
                            self.assertRaises(ValueError):
                        PaperEngine(state=state)


class ApplyOpenPositionControlsTests(unittest.TestCase):
    def test_future_settings_do_not_change_open_position_until_explicit_apply(self):
        original_config = PaperConfig(require_toward_strike=False, stop_loss=.17,
                                      max_hold_seconds=250, contracts=7, min_exit_price=.82,
                                      dynamic_exit_min=.82, dynamic_exit_max=.93)
        engine = opened(original_config)
        original_position = copy.deepcopy(engine.position)
        original_entry = copy.deepcopy(engine.events[0])
        original_cash = engine.cash
        future = replace(original_config, hold_to_settlement=True, exit_on_stop_loss=False,
                         exit_on_reversal=False, exit_on_max_hold=False, exit_before_expiry=False,
                         stop_loss=.3, max_hold_seconds=500, contracts=1, min_exit_price=.85,
                         dynamic_exit_min=.85, dynamic_exit_max=.95)
        engine.set_config(future)
        self.assertEqual(engine.snapshot()["position"]["exit_controls"], control_values(original_config))
        self.assertTrue(engine.apply_exit_controls_to_position(ticker=TICKER,
                        opened_at=original_position["opened_at"], wall_time=EPOCH + 30))
        frozen = engine._position_config()
        self.assertEqual(control_values(frozen), control_values(future))
        for name, value in asdict(original_config).items():
            if name not in EXIT_CONTROL_FIELDS:
                self.assertEqual(getattr(frozen, name), value)
        self.assertEqual(engine.position["contracts"], 7)
        self.assertEqual(engine.position["entry_price"], original_position["entry_price"])
        self.assertEqual(engine.position["exit_target"], original_position["exit_target"])
        self.assertEqual(engine.cash, original_cash)
        self.assertEqual(engine.realized_pnl, 0)
        self.assertEqual(engine.event_seq, 1)
        self.assertEqual(engine.events[0], original_entry)
        self.assertEqual(engine.position["exit_controls_revision"], 1)
        self.assertEqual(engine.position["exit_controls_updated_at"], iso(EPOCH + 30).replace("+00:00", ".000+00:00"))
        self.assertEqual(engine.position["exit_controls_config_revision"], engine.config_revision)
        self.assertFalse(engine.apply_exit_controls_to_position(ticker=TICKER,
                         opened_at=original_position["opened_at"], wall_time=EPOCH + 31))
        self.assertEqual(engine.position["exit_controls_revision"], 1)
        self.assertEqual(PaperEngine(state=engine.dump_state()).dump_state(), engine.dump_state())
        event = engine.step({}, book(bid=.95), now=32, wall_time=EPOCH + 32)[0]
        self.assertEqual(event["exit_controls"], control_values(future))
        self.assertEqual(event["exit_controls_revision"], 1)
        self.assertEqual(engine.events[0], original_entry)

    def test_explicit_apply_is_allowed_for_legacy_position_without_rules(self):
        state = opened().dump_state()
        del state["position"]["rules"]
        engine = PaperEngine(state=state)
        engine.set_config(replace(engine.config, hold_to_settlement=True))
        self.assertTrue(engine.apply_exit_controls_to_position(ticker=TICKER,
                        opened_at=state["position"]["opened_at"], wall_time=EPOCH + 25))
        self.assertFalse(engine._position_config().require_toward_strike)
        self.assertTrue(engine._position_config().hold_to_settlement)

    def test_identity_mismatch_or_invalid_time_reject_without_any_mutation(self):
        engine = opened()
        engine.set_config(replace(engine.config, hold_to_settlement=True))
        before = engine.dump_state()
        opened_at = engine.position["opened_at"]
        for arguments in ({"ticker": "KXBTC15M-DIFFERENT", "opened_at": opened_at},
                          {"ticker": TICKER, "opened_at": opened_at + 1},
                          {"ticker": TICKER, "opened_at": str(opened_at)},
                          {"ticker": TICKER, "opened_at": opened_at, "wall_time": float("nan")},
                          {"ticker": TICKER, "opened_at": opened_at, "wall_time": 1e100}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                engine.apply_exit_controls_to_position(**{"wall_time": EPOCH + 30, **arguments})
            self.assertEqual(engine.dump_state(), before)
        engine.position = None
        with self.assertRaisesRegex(ValueError, "open position changed"):
            engine.apply_exit_controls_to_position(ticker=TICKER, opened_at=opened_at, wall_time=EPOCH + 30)


if __name__ == "__main__":
    unittest.main()
