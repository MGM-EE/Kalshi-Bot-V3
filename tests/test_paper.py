"""Synthetic deterministic market scenarios. No network or real orders."""
import copy
import json
import unittest
from datetime import datetime, timezone

from feedboard.paper import PaperConfig, PaperEngine, taker_fee


EPOCH = 1_800_000_000.
TICKER = "KXBTC15M-TEST"


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def feeds(mid=65000, update=1, direction=1):
    return {
        "kalshi": {"status": "live", "age_ms": 0, "updates": update, "data": {"markets": []}},
        "kraken": {"status": "live", "age_ms": 0, "updates": update, "transport": "websocket",
                   "data": {"symbol": "BTC/USD", "bid": mid - .5, "ask": mid + .5, "last": mid,
                            "flow": {"buy_usd": 9000 if direction == 1 else 1000,
                                     "sell_usd": 1000 if direction == 1 else 9000,
                                     "large_buy_usd": 2000 if direction == 1 else 0,
                                     "large_sell_usd": 0 if direction == 1 else 2000,
                                     "count": 20, "age_ms": 0, "window_seconds": 20}}},
    }


def book(version=1, **values):
    return {"ticker": TICKER, "status": "live", "age_ms": 0, "version": version,
            "yes": [[.48, 20]], "no": [[.50, 20]], "close_time": iso(EPOCH + 900),
            "market_status": "open", "settlement_result": None, **values}


def warm(engine=None, direction=1, **book_values):
    engine = engine or PaperEngine()
    events = []
    for second in range(21):
        events.extend(engine.step(feeds(65000 + direction * second * 4, second + 1, direction),
                                  book(second + 1, **book_values), now=float(second), wall_time=EPOCH + second))
    return engine, events


class PaperEntryTests(unittest.TestCase):
    def test_up_entry_uses_opposite_bid_complement_and_pays_fee(self):
        engine, events = warm()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual((event["type"], event["side"], event["contracts"], event["price"]),
                         ("entry", "up", 5, .50))
        self.assertEqual(engine.cash, 1000 - 2.5 - taker_fee(5, .5))
        self.assertEqual(engine.position["entry_price"], .5)
        self.assertEqual(engine.snapshot()["mode"], "paper")

    def test_down_entry_uses_yes_bid_complement(self):
        engine, events = warm(direction=-1)
        self.assertEqual(events[0]["side"], "down")
        self.assertEqual(events[0]["price"], .52)
        self.assertEqual(engine.position["contracts"], 5)

    def test_flat_prices_finish_warmup_without_triggering_entry(self):
        engine = PaperEngine()
        for second in range(21):
            self.assertEqual(engine.step(feeds(update=second), book(second), now=second,
                                         wall_time=EPOCH + second), [])
            self.assertEqual(engine.snapshot()["status"], "warming_up" if second < 20 else "watching")
        self.assertIsNone(engine.position)

    def test_warmup_requires_elapsed_time_and_at_least_five_distinct_updates(self):
        engine = PaperEngine()
        # Fresh ages alone cannot turn one repeatedly observed quote into a history.
        for second in range(21):
            self.assertEqual(engine.step(feeds(), book(second), now=second,
                                         wall_time=EPOCH + second), [])
        self.assertEqual(engine.snapshot()["status"], "warming_up")
        engine, events = warm()
        self.assertEqual(len(events), 1)

    def test_entry_guards_block_stale_feeds_wrong_transport_or_symbol(self):
        cases = [("kalshi", "status", "stale"), ("kalshi", "age_ms", 3001),
                 ("kraken", "status", "retrying"), ("kraken", "age_ms", 3001),
                 ("kraken", "transport", "rest")]
        for feed_name, field, value in cases:
            engine = PaperEngine()
            for second in range(21):
                data = feeds(65000 + second * 4, second)
                data[feed_name][field] = value
                self.assertEqual(engine.step(data, book(second), now=second, wall_time=EPOCH + second), [])
            self.assertIsNone(engine.position)
        engine, events = warm(ticker="OTHER-TEST")
        self.assertEqual(events, [])
        self.assertEqual(engine.snapshot()["status"], "paused")

    def test_stale_flow_opposite_large_prints_and_weak_momentum_do_not_enter(self):
        for condition in ("stale", "opposite_large", "weak", "missing", "short_window"):
            engine = PaperEngine()
            for second in range(21):
                data = feeds(65000 + second * (.01 if condition == "weak" else 4), second)
                flow = data["kraken"]["data"]["flow"]
                if condition == "stale":
                    flow["age_ms"] = 3001
                elif condition == "opposite_large":
                    flow.update(large_buy_usd=0, large_sell_usd=2000)
                elif condition == "missing":
                    del data["kraken"]["data"]["flow"]
                elif condition == "short_window":
                    flow["window_seconds"] = 3
                self.assertEqual(engine.step(data, book(second), now=second, wall_time=EPOCH + second), [])
            self.assertIsNone(engine.position)

    def test_entry_requires_open_market_time_narrow_spread_and_full_depth(self):
        cases = [{"close_time": iso(EPOCH + 100)}, {"market_status": "closed"},
                 {"settlement_result": "yes"},
                 {"yes": [[.40, 20]]}, {"no": [[.50, 2]]},
                 {"no": [[.50, 2], [.48, 8]]}, {"no": [[.2, 20]], "yes": [[.78, 20]]},
                 {"status": "retrying"}, {"age_ms": 3001}]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                engine, events = warm(**overrides)
                self.assertEqual(events, [])
                self.assertIsNone(engine.position)

    def test_entry_sweeps_within_one_cent_and_charges_each_level_fee(self):
        engine, events = warm(no=[[.50, 2], [.49, 3]])
        self.assertAlmostEqual(events[0]["price"], .506)
        self.assertAlmostEqual(engine.cash, 1000 - (2 * .5 + 3 * .51) - taker_fee(2, .5) - taker_fee(3, .51))

    def test_unsupported_strategy_configuration_rejected(self):
        for values in ({"symbol": "ETH/USD"}, {"series": "OTHER"}, {"contracts": 0},
                       {"initial_cash": float("nan")}, {"warmup_seconds": 5}):
            with self.assertRaises(ValueError):
                PaperConfig(**values)


class PaperExitTests(unittest.TestCase):
    def test_hard95_exits_at_observed_bid_and_net_pnl_is_correct(self):
        for direction, side, bids_key in ((1, "up", "yes"), (-1, "down", "no")):
            engine, _ = warm(direction=direction)
            entry = engine.position["entry_price"]
            entry_fee = engine.position["entry_fee_remaining"]
            event = engine.step(feeds(65000 + direction * 84, 22, direction),
                                book(22, **{bids_key: [[.96, 5]]}), now=21, wall_time=EPOCH + 21)[0]
            expected = 5 * (.96 - entry) - entry_fee - taker_fee(5, .96)
            self.assertEqual(event["reason"], "hard_95c")
            self.assertEqual(event["side"], side)
            self.assertAlmostEqual(event["pnl"], expected)
            self.assertAlmostEqual(engine.realized_pnl, expected)
            self.assertAlmostEqual(engine.cash, 1000 + expected)
            self.assertEqual(engine.trade_count, 1)
            self.assertEqual(engine.wins, 1)
            self.assertIsNone(engine.position)

    def test_profit_fills_stop_at_trigger_partial_remainder_waits_for_revision(self):
        engine, _ = warm()
        # Entry is already one fill on version21. Reusing it cannot fabricate an exit.
        self.assertEqual(engine.step(feeds(), book(21, yes=[[.95, 5]]), now=20, wall_time=EPOCH + 20), [])
        event = engine.step(feeds(), book(22, yes=[[.96, 2], [.94, 8]]), now=21, wall_time=EPOCH + 21)[0]
        self.assertEqual(event["contracts"], 2)
        self.assertEqual(engine.position["contracts"], 3)
        self.assertEqual(engine.step(feeds(), book(22, yes=[[.99, 20]]), now=22, wall_time=EPOCH + 22), [])
        self.assertEqual(engine.position["contracts"], 3)
        event2 = engine.step(feeds(), book(23, yes=[[.95, 3]]), now=23, wall_time=EPOCH + 23)[0]
        self.assertEqual(event2["contracts"], 3)
        self.assertEqual(engine.trade_count, 1)
        self.assertIsNone(engine.position)
        self.assertAlmostEqual(engine.cash, 1000 + engine.realized_pnl)

    def test_fresh_book_allows_exit_with_kraken_and_listing_unavailable(self):
        engine, _ = warm()
        data = feeds()
        for feed in data.values():
            feed.update(status="retrying", age_ms=100000)
        event = engine.step(data, book(22, yes=[[.95, 5]]), now=21, wall_time=EPOCH + 21)[0]
        self.assertEqual(event["reason"], "hard_95c")

    def test_stale_missing_and_wrong_market_books_never_fill(self):
        for overrides in ({"status": "retrying"}, {"age_ms": 3001}, {"yes": []},
                          {"ticker": "KXBTC15M-NEXT"}):
            engine, _ = warm()
            current = {"yes": [[.99, 50]], **overrides}
            self.assertEqual(engine.step(feeds(), book(22, **current), now=21, wall_time=EPOCH + 21), [])
            self.assertEqual(engine.position["contracts"], 5)
            self.assertIsNone(engine.snapshot()["position"]["mark"])
            self.assertIsNone(engine.snapshot()["equity"])

    def test_target_adapts_between80_and95(self):
        engine, _ = warm()
        old_target = engine.position["exit_target"]
        self.assertTrue(.80 <= old_target <= .95)
        # Gentle reverse pressure lowers target before reaching reversal exit threshold.
        data = feeds(65080, 22)
        data["kraken"]["data"]["flow"].update(buy_usd=1000, sell_usd=9000,
                                                large_buy_usd=0, large_sell_usd=9000)
        engine.step(data, book(22, yes=[[.48, 1]], no=[[.50, 500]]), now=21, wall_time=EPOCH + 21)
        self.assertLess(engine.position["exit_target"], old_target)
        for price in (65080, 66000, 68000):
            engine.step(feeds(price, price), book(int(price)), now=22, wall_time=EPOCH + 22)
            self.assertLessEqual(engine.position["exit_target"], .95)
            self.assertGreaterEqual(engine.position["exit_target"], .80)

    def test_dynamic_target_uses_bid_not_last_or_listing_price(self):
        engine, _ = warm()
        data = feeds(65084, 22)
        data["kalshi"]["data"]["markets"] = [{"ticker": TICKER, "last": 1., "yes_bid": 1.}]
        self.assertEqual(engine.step(data, book(22, yes=[[.79, 20]]), now=21, wall_time=EPOCH + 21), [])
        target = engine.position["exit_target"]
        event = engine.step(feeds(65088, 23), book(23, yes=[[target, 20]]), now=22, wall_time=EPOCH + 22)[0]
        self.assertEqual(event["reason"], "dynamic_target")
        self.assertAlmostEqual(event["price"], target)

    def test_stop_can_exit_below80_with_real_depth_and_loss(self):
        engine, _ = warm()
        event = engine.step(feeds(), book(22, yes=[[.37, 2], [.36, 3]]), now=21, wall_time=EPOCH + 21)[0]
        self.assertEqual(event["reason"], "stop_loss")
        self.assertLess(event["pnl"], 0)
        self.assertEqual(engine.wins, 0)
        self.assertEqual(engine.trade_count, 1)

    def test_time_and_expiry_risk_exit(self):
        for when, reason in ((EPOCH + 200, "max_hold"), (EPOCH + 875, "expiry_risk")):
            engine, _ = warm()
            event = engine.step(feeds(), book(22), now=when-EPOCH, wall_time=when)[0]
            self.assertEqual(event["reason"], reason)

    def test_no_guessed_settlement_on_close_or_rollover_official_result_required(self):
        engine, _ = warm()
        late = EPOCH + 901
        self.assertEqual(engine.step(feeds(), book(22, yes=[[.99, 50]]), now=901, wall_time=late), [])
        self.assertEqual(engine.snapshot()["status"], "awaiting_settlement")
        self.assertEqual(engine.step(feeds(), book(23, ticker="KXBTC15M-NEXT", settlement_result="yes"),
                                     now=902, wall_time=late + 1), [])
        self.assertIsNotNone(engine.position)
        event = engine.step(feeds(), book(24, settlement_result="yes", yes=[]),
                            now=903, wall_time=late + 2)[0]
        self.assertEqual((event["type"], event["price"]), ("settlement", 1.0))
        self.assertAlmostEqual(engine.cash, 1000 + 2.5 - taker_fee(5, .5))
        self.assertIsNone(engine.position)

    def test_closed_market_quote_cannot_fill_even_before_scheduled_close(self):
        engine, _ = warm()
        self.assertEqual(engine.step({}, book(22, market_status="closed", yes=[[.99, 50]]),
                                     now=21, wall_time=EPOCH + 21), [])
        self.assertEqual(engine.snapshot()["status"], "awaiting_settlement")
        self.assertIsNone(engine.snapshot()["position"]["mark"])

    def test_down_settlement_correct_for_both_outcomes(self):
        for result, price in (("yes", 0.), ("no", 1.)):
            engine, _ = warm(direction=-1)
            event = engine.step({}, book(22, settlement_result=result), now=901, wall_time=EPOCH + 901)[0]
            self.assertEqual(event["price"], price)
            expected = 1000 - 5 * .52 - taker_fee(5, .52) + 5 * price
            self.assertAlmostEqual(engine.cash, expected)

    def test_reversal_exits_on_fresh_aggressive_opposite_flow(self):
        engine, _ = warm()
        for second in range(21, 26):
            events = engine.step(feeds(65080 - (second - 20) * 12, second + 1, -1),
                                 book(second + 1), now=second, wall_time=EPOCH + second)
            if events:
                self.assertEqual(events[0]["reason"], "momentum_reversal")
                break
        else:
            self.fail("reversal never exited")


class PaperPersistenceTests(unittest.TestCase):
    def test_restore_keeps_open_position_cash_ids_and_consumed_revision(self):
        engine, _ = warm()
        state = json.loads(json.dumps(engine.dump_state()))
        restored = PaperEngine(state=state)
        self.assertEqual(restored.dump_state(), state)
        self.assertEqual(restored.step({}, book(21, yes=[[.95, 5]]), now=0, wall_time=EPOCH + 22), [])
        event = restored.step({}, book(22, yes=[[.95, 5]]), now=1, wall_time=EPOCH + 23)[0]
        self.assertEqual(event["id"], 2)
        self.assertEqual(restored.trade_count, 1)
        self.assertEqual(len(restored.events), 2)

    def test_restore_resets_warmup_and_preserves_cooldown(self):
        engine, _ = warm()
        engine.step({}, book(22, yes=[[.95, 5]]), now=21, wall_time=EPOCH + 21)
        restored = PaperEngine(state=engine.dump_state())
        self.assertEqual(restored.cooldown_until, EPOCH + 41)
        self.assertEqual(restored.step(feeds(66000), book(23), now=1, wall_time=EPOCH + 22), [])
        self.assertEqual(restored.snapshot()["status"], "warming_up")
        self.assertIsNone(restored.position)

    def test_corrupt_restore_is_rejected(self):
        engine, _ = warm()
        valid = engine.dump_state()
        variants = [None, {}, {**valid, "cash": float("nan")}, {**valid, "cash": -1},
                    {**valid, "trade_count": True}, {**valid, "wins": 2},
                    {**valid, "last_book_versions": {TICKER: "bad"}},
                    {**valid, "events": [{"id": 1}]},
                    {**valid, "position": {**valid["position"], "contracts": 0}},
                    {**valid, "position": {**valid["position"], "entry_price": "NaN"}}]
        for state in variants:
            if state is None:
                continue  # None intentionally means a fresh account.
            with self.subTest(state=state):
                with self.assertRaises(ValueError):
                    PaperEngine(state=state)

    def test_snapshots_and_dumps_do_not_allow_external_mutation(self):
        engine, _ = warm()
        view, state = engine.snapshot(), engine.dump_state()
        view["position"]["contracts"] = 0
        state["events"][0]["side"] = "bad"
        self.assertEqual(engine.position["contracts"], 5)
        self.assertEqual(engine.events[0]["side"], "up")

    def test_history_is_bounded(self):
        engine = PaperEngine()
        position = {"ticker": TICKER, "side": "up"}
        for n in range(100):
            engine._emit("exit", position, .5, 1, -.1, "synthetic", EPOCH + n)
        self.assertEqual(len(engine.snapshot()["events"]), 20)
        self.assertEqual(engine.snapshot()["last_event"]["id"], 100)


if __name__ == "__main__":
    unittest.main()
