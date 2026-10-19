"""Synthetic public-feed parsing, sizing, and stale-signal checks."""

import math
import unittest
from datetime import datetime, timezone

from feedboard.marketdata import TradeFlow, normalize_orderbook


def trade(trade_id=1, side="buy", price=50_000, qty=1, **values):
    return {"symbol": "BTC/USD", "side": side, "price": price,
            "qty": qty, "trade_id": trade_id, **values}


def update(*rows, kind="update"):
    return {"channel": "trade", "type": kind, "data": list(rows)}


def iso_time(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


class OrderbookTests(unittest.TestCase):
    def test_current_dollar_prices_keep_fractional_contract_sizes(self):
        self.assertEqual(normalize_orderbook({"orderbook_fp": {
            "yes_dollars": [["0.0710", "1009.00"], ["0.0750", "914.69"]],
            "no_dollars": [["0.9240", "1645.37"]],
        }}), {"yes": [[0.075, 914.69], [0.071, 1009.0]], "no": [[0.924, 1645.37]]})

    def test_legacy_prices_are_integer_cents_and_current_fields_take_precedence(self):
        self.assertEqual(normalize_orderbook({"orderbook": {
            "yes": [[71, 12], [72, "1.25"]], "no": [[25, 7]],
        }}), {"yes": [[0.72, 1.25], [0.71, 12.0]], "no": [[0.25, 7.0]]})
        self.assertEqual(normalize_orderbook({
            "orderbook_fp": {"yes_dollars": None},
            "orderbook": {"yes": [[50, 100]], "no": [[50, 100]]},
        }), {"yes": [], "no": []})

    def test_only_five_highest_actual_levels_remain(self):
        book = normalize_orderbook({"orderbook_fp": {
            "yes_dollars": [[str(p / 100), str(p)] for p in range(10, 90)],
            "no_dollars": [],
        }})
        self.assertEqual(book["yes"], [[p / 100, float(p)] for p in range(89, 84, -1)])
        self.assertEqual(book["no"], [])

    def test_invalid_prices_quantities_and_level_shapes_are_omitted(self):
        invalid = [None, {}, True, "bad", "NaN", "Infinity", -1]
        rows = [[p, 1] for p in invalid] + [[0.5, q] for q in invalid + [0]]
        rows += [[1.01, 1], [], [0.5], [0.5, 1, 2], {}, [0, 1], [1, 2], [0.5, "0.25"]]
        self.assertEqual(normalize_orderbook({"orderbook_fp": {"yes_dollars": rows}}),
                         {"yes": [[1.0, 2.0], [0.5, 0.25], [0.0, 1.0]], "no": []})
        self.assertEqual(normalize_orderbook({"orderbook": {"yes": [[12.5, 100]]}})["yes"], [])

    def test_duplicate_snapshot_prices_never_multiply_liquidity(self):
        self.assertEqual(normalize_orderbook({"orderbook_fp": {
            "yes_dollars": [[0.5, 12], [0.5, 4], [0.5, 20]],
        }})["yes"], [[0.5, 4.0]])

    def test_crossed_outcome_bids_cannot_create_fictional_profit(self):
        for book in ({"orderbook_fp": {"yes_dollars": [["0.55", "10"]],
                                      "no_dollars": [["0.46", "10"]]}},
                     {"orderbook": {"yes": [[90, 10]], "no": [[20, 10]]}}):
            with self.subTest(book=book), self.assertRaisesRegex(ValueError, "crossed"):
                normalize_orderbook(book)
        locked = normalize_orderbook({"orderbook_fp": {
            "yes_dollars": [["0.55", "10"]], "no_dollars": [["0.45", "10"]],
        }})
        self.assertEqual(locked["yes"], [[0.55, 10.0]])
        # With no opposite bid there is no invented ask or cross to reject.
        self.assertEqual(normalize_orderbook({"orderbook_fp": {
            "yes_dollars": [["0.99", "10"]], "no_dollars": [],
        }}), {"yes": [[0.99, 10.0]], "no": []})

    def test_invalid_response_fails_instead_of_inventing_a_book(self):
        for payload in (None, [], {}, {"orderbook_fp": None}, {"orderbook": []},
                        {"orderbook_fp": {"yes_dollars": {}}}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                normalize_orderbook(payload)
        self.assertEqual(normalize_orderbook({"orderbook_fp": {}}), {"yes": [], "no": []})


class TradeFlowTests(unittest.TestCase):
    def test_buy_sell_alignment_and_large_print_threshold(self):
        flow = TradeFlow()
        flow.add_payload(update(trade(1, "buy", qty=1), trade(2, "buy", qty=2),
                                trade(3, "sell", qty=3), trade(4, "sell", qty=0.5)), "BTC/USD", 10)
        self.assertEqual(flow.snapshot(10.25), {
            "buy_usd": 150_000, "sell_usd": 175_000, "large_buy_usd": 100_000,
            "large_sell_usd": 150_000, "count": 4, "age_ms": 250, "window_seconds": 20.0,
        })

    def test_snapshot_history_other_channels_and_other_symbols_are_ignored(self):
        flow = TradeFlow()
        for payload in (update(trade(), kind="snapshot"), {"channel": "heartbeat"},
                        update(trade(symbol="ETH/USD")), {"channel": "trade", "type": "update", "data": {}},
                        {"channel": "ticker", "type": "update", "data": [trade()]}, [], None):
            flow.add_payload(payload, "BTC/USD", 10)
        self.assertEqual(flow.snapshot(10)["count"], 0)
        self.assertIsNone(flow.snapshot(10)["age_ms"])

    def test_invalid_rows_never_become_flow(self):
        flow = TradeFlow()
        invalid = [None, True, "NaN", "Infinity", -1, 0, {}, []]
        rows = [trade(index + 1, price=value) for index, value in enumerate(invalid)]
        rows += [trade(index + 20, qty=value) for index, value in enumerate(invalid)]
        rows += [trade(100, side="unknown"), trade(None), trade(True), trade(1.5),
                 trade("bad"), trade(10 ** 50), trade(100, price=1e308, qty=1e308), None, {}]
        flow.add_payload(update(*rows), "BTC/USD", 10)
        self.assertEqual(flow.snapshot(10)["count"], 0)

    def test_duplicates_within_message_and_after_reconnect_are_counted_once(self):
        flow = TradeFlow()
        flow.add_payload(update(trade(), trade()), "BTC/USD", 10)
        flow.add_payload(update(trade(), kind="snapshot"), "BTC/USD", 11)
        flow.add_payload(update(trade("0001"), trade(2, qty=2)), "BTC/USD", 12)
        view = flow.snapshot(12)
        self.assertEqual(view["buy_usd"], 150_000)
        self.assertEqual(view["count"], 2)

    def test_replayed_id_does_not_refresh_flow_age(self):
        flow = TradeFlow()
        flow.add_payload(update(trade()), "BTC/USD", 10)
        flow.add_payload(update(trade()), "BTC/USD", 15)
        self.assertEqual(flow.snapshot(15)["age_ms"], 5000)

    def test_window_expires_without_more_messages(self):
        flow = TradeFlow()
        flow.add_payload(update(trade()), "BTC/USD", 10)
        self.assertEqual(flow.snapshot(29.999)["count"], 1)
        self.assertEqual(flow.snapshot(30)["count"], 0)
        self.assertEqual(flow.snapshot(35)["buy_usd"], 0)
        self.assertEqual(flow.snapshot(35)["age_ms"], 25_000)

    def test_newer_buckets_survive_old_bucket_expiry(self):
        flow = TradeFlow()
        flow.add_payload(update(trade(1)), "BTC/USD", 10)
        flow.add_payload(update(trade(2, side="sell")), "BTC/USD", 12)
        self.assertEqual(flow.snapshot(30)["buy_usd"], 0)
        self.assertEqual(flow.snapshot(30)["sell_usd"], 50_000)
        self.assertEqual(flow.snapshot(32)["count"], 0)

    def test_wall_clock_validation_rejects_old_future_and_missing_timestamps(self):
        flow = TradeFlow()
        wall = 1_700_000_000
        rows = [trade(1, timestamp=iso_time(wall - 21)), trade(2, timestamp=iso_time(wall + 6)),
                trade(3, timestamp="bad"), trade(4), trade(5, timestamp="2023-11-14T00:00:00"),
                trade(6, timestamp=iso_time(wall - 1))]
        flow.add_payload(update(*rows), "BTC/USD", 100, wall_time=wall)
        self.assertEqual(flow.snapshot(100)["count"], 1)
        self.assertEqual(flow.snapshot(100)["age_ms"], 1000)
        self.assertEqual(flow.snapshot(119)["count"], 0)

    def test_out_of_order_exchange_events_keep_latest_age_and_expire_correctly(self):
        flow = TradeFlow()
        wall = 1_700_000_000
        flow.add_payload(update(trade(1, timestamp=iso_time(wall)),
                                trade(2, timestamp=iso_time(wall - 5))), "BTC/USD", 100, wall_time=wall)
        self.assertEqual(flow.snapshot(100)["age_ms"], 0)
        self.assertEqual(flow.snapshot(115)["count"], 1)

    def test_flow_and_replay_memory_remain_bounded_during_a_burst(self):
        flow = TradeFlow()
        for index in range(10_000):
            flow.add_payload(update(trade(index, price=1, qty=1)), "BTC/USD", index / 1000)
        self.assertLessEqual(len(flow._buckets), flow.max_buckets)
        self.assertLessEqual(len(flow._trade_ids), flow.max_trade_ids)
        self.assertEqual(flow.snapshot(10)["count"], 10_000)
        self.assertEqual(flow.snapshot(80)["count"], 0)
        self.assertEqual(len(flow._trade_ids), 0)

    def test_all_twenty_seconds_use_at_most_eighty_one_buckets(self):
        flow = TradeFlow()
        for index in range(25_000):
            flow.add_payload(update(trade(index, price=1, qty=1)), "BTC/USD", index / 1000)
        self.assertLessEqual(len(flow._buckets), 81)
        self.assertLessEqual(flow.snapshot(25)["count"], 20_000)

    def test_aggregate_numbers_stay_finite_even_for_extreme_finite_prices(self):
        flow = TradeFlow()
        for index in range(100):
            flow.add_payload(update(trade(index, price=1e306, qty=1)), "BTC/USD", 10 + index / 10)
        for field in ("buy_usd", "sell_usd", "large_buy_usd", "large_sell_usd"):
            self.assertTrue(math.isfinite(flow.snapshot(20)[field]))

    def test_nonfinite_or_regressing_monotonic_times_are_ignored(self):
        flow = TradeFlow()
        flow.add_payload(update(trade(1)), "BTC/USD", 10)
        for now in (float("nan"), float("inf"), True, 9):
            flow.add_payload(update(trade(2)), "BTC/USD", now)
        self.assertEqual(flow.snapshot(10)["count"], 1)


if __name__ == "__main__":
    unittest.main()
