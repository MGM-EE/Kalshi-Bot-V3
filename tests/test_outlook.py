"""Deterministic model scenarios: no network requests or trading behavior."""
import copy
import json
import math
import unittest
from datetime import datetime, timezone

from feedboard.outlook import SettlementOutlook


EPOCH = 1_800_000_000.0


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def quote(mid=100000):
    return {"symbol": "BTC/USD", "bid": mid - 0.5, "ask": mid + 0.5}


def market(**changes):
    return {"status": "live", "age_ms": 0, "ticker": "KXBTC15M-TEST",
            "strike_price": 100000, "strike_source": "kalshi.floor_strike",
            "close_time": iso(EPOCH + 900), "market_status": "open",
            "settlement_result": None, **changes}


def feed(mid=100000, **changes):
    return {"status": "live", "age_ms": 0, "transport": "websocket",
            "data": quote(mid), **changes}


def warm(mid=100000, end=300, price=None):
    outlook = SettlementOutlook()
    for second in range(end - 60, end + 1):
        current = price(second) if price else mid
        outlook.observe(quote(current), now=second, wall_time=EPOCH + second)
    return outlook


def snapshot(outlook, mid=100000, now=300, book=None, kraken=None, **changes):
    return outlook.snapshot(book if book is not None else market(),
                            kraken if kraken is not None else feed(mid),
                            now=now, wall_time=EPOCH + now, **changes)


class OutlookEstimateTests(unittest.TestCase):
    def test_warmup_requires_time_and_distinct_observation_buckets(self):
        outlook = SettlementOutlook()
        for index in range(1000):
            second = index / 1000
            outlook.observe(quote(), now=second, wall_time=EPOCH + second)
        result = snapshot(outlook, now=0.999)
        self.assertEqual(result["status"], "warming_up")
        self.assertIsNone(result["up_percent"])
        self.assertEqual(result["history"], [])
        self.assertEqual(len(outlook._quotes), 1)
        self.assertEqual(snapshot(warm(), now=300)["status"], "live")

    def test_direction_is_symmetric_and_percentages_are_complements(self):
        high, low = 100000 * math.exp(0.0005), 100000 * math.exp(-0.0005)
        up = snapshot(warm(high), high)
        down = snapshot(warm(low), low)
        neutral = snapshot(warm())
        self.assertEqual(up["up_percent"], down["down_percent"])
        for result in (up, down, neutral):
            self.assertEqual(result["up_percent"] + result["down_percent"], 100)
        self.assertEqual(up["favored"], "up")
        self.assertEqual(down["favored"], "down")
        self.assertEqual(neutral["up_percent"], 50)
        self.assertIsNone(neutral["favored"])

    def test_same_price_distance_gains_confidence_with_less_time_remaining(self):
        mid = 100020
        early = snapshot(warm(mid, end=120), mid, now=120)
        late = snapshot(warm(mid, end=850), mid, now=850)
        self.assertGreater(late["up_percent"], early["up_percent"])
        self.assertEqual(early["elapsed_seconds"], 120)
        self.assertEqual(late["remaining_seconds"], 50)
        # Listing open_time is intentionally ignored: these markets' listing
        # dates are not the start of their fifteen-minute settlement window.
        self.assertEqual(snapshot(warm(), book=market(open_time=iso(EPOCH - 86400)))["open_time"],
                         iso(EPOCH).replace("+00:00", ".000+00:00"))

    def test_high_realized_volatility_reduces_distance_confidence(self):
        mid = 100050
        quiet = snapshot(warm(mid), mid)
        turbulent = warm(mid, price=lambda second: mid + (200 if second % 2 else 0))
        uncertain = snapshot(turbulent, mid)
        self.assertGreater(quiet["up_percent"], uncertain["up_percent"])
        self.assertGreaterEqual(uncertain["up_percent"], 50)

    def test_momentum_bias_is_short_and_bounded_not_fifteen_minute_projection(self):
        rising = warm(price=lambda second: 100000 * math.exp(0.0001 * (second - 300)))
        falling = warm(price=lambda second: 100000 * math.exp(-0.0001 * (second - 300)))
        up, down = snapshot(rising), snapshot(falling)
        self.assertGreater(up["up_percent"], 50)
        self.assertLessEqual(up["up_percent"], 60)
        self.assertEqual(up["up_percent"], down["down_percent"])
        self.assertGreaterEqual(down["up_percent"], 40)

    def test_live_confidence_never_claims_certainty(self):
        for mid, expected in ((1000000, 95), (1000, 5)):
            result = snapshot(warm(mid, end=899), mid, now=899)
            self.assertEqual(result["status"], "live")
            self.assertEqual(result["up_percent"], expected)
        # Even a second before expiry, the index uncertainty floor prevents
        # tiny spot differences from becoming artificial certainty.
        small_edge = snapshot(warm(100001, end=899), 100001, now=899)
        self.assertLess(small_edge["up_percent"], 60)


class OutlookInputTests(unittest.TestCase):
    def test_invalid_or_unverified_contract_inputs_do_not_estimate(self):
        outlook = warm()
        changes = [{"ticker": "OTHER-TEST"}, {"series": "OTHER"},
                   {"strike_price": None}, {"strike_price": 0},
                   {"strike_price": True}, {"strike_price": float("nan")},
                   {"strike_source": "inferred"}, {"close_time": "bad"},
                   {"close_time": "2026-01-01T00:00:00"},
                   {"close_time": iso(EPOCH + 1800)}, {"market_status": "initialized"}]
        for values in changes:
            with self.subTest(values=values):
                result = snapshot(outlook, book=market(**values))
                self.assertEqual(result["status"], "unavailable")
                self.assertIsNone(result["up_percent"])
                json.dumps(result, allow_nan=False)

    def test_stale_book_or_spot_and_rest_fallback_hide_percentages(self):
        outlook = warm()
        for stale in ({"status": "retrying"}, {"age_ms": 3001},
                      {"age_ms": -1}, {"age_ms": float("nan")}):
            with self.subTest(stale=stale):
                self.assertEqual(snapshot(outlook, book=market(**stale))["status"], "stale")
                result = snapshot(outlook, kraken=feed(**stale))
                self.assertEqual(result["status"], "stale")
                self.assertIsNone(result["up_percent"])
        self.assertEqual(snapshot(outlook, kraken=feed(transport="rest"))["status"], "stale")
        # A caller cannot keep a forgotten observation fresh with an age_ms=0.
        self.assertEqual(snapshot(outlook, now=304)["status"], "stale")

    def test_invalid_quotes_are_not_ingested_or_shown(self):
        outlook = warm()
        bad_quotes = [{}, {"bid": 0, "ask": 1}, {"bid": 2, "ask": 1},
                      {"bid": True, "ask": 1}, {"bid": 1, "ask": float("inf")},
                      {"symbol": "ETH/USD", "bid": 100, "ask": 101}]
        for bad in bad_quotes:
            with self.subTest(bad=bad):
                outlook.observe(bad, now=301, wall_time=EPOCH + 301)
                result = snapshot(outlook, kraken=feed(data=bad))
                self.assertEqual(result["status"], "unavailable")
                self.assertIsNone(result["up_percent"])
        self.assertEqual(snapshot(outlook)["status"], "live")
        self.assertEqual(snapshot(outlook, 100001)["status"], "unavailable")

    def test_rest_wrong_symbol_and_duplicate_timestamp_cannot_warm_model(self):
        outlook = SettlementOutlook()
        for second in range(61):
            outlook.observe(quote(), now=second, wall_time=EPOCH + second, transport="rest")
            outlook.observe(quote(), now=second, wall_time=EPOCH + second, symbol="ETH/USD")
        self.assertEqual(snapshot(outlook, now=60)["status"], "warming_up")
        for second in range(61):
            outlook.observe(quote(), now=0, wall_time=EPOCH)
        self.assertEqual(len(outlook._quotes), 1)
        self.assertEqual(snapshot(outlook, now=0)["status"], "warming_up")

    def test_gap_requires_fresh_warmup_and_invalid_clocks_are_ignored(self):
        outlook = warm()
        outlook.observe(quote(), now=200, wall_time=EPOCH + 200)
        self.assertEqual(snapshot(outlook)["status"], "live")
        self.assertEqual(snapshot(outlook, now=299)["status"], "unavailable")
        invalid_now = outlook.snapshot(market(), feed(), now=float("nan"), wall_time=EPOCH)
        self.assertEqual(invalid_now["status"], "unavailable")
        shifted = outlook.snapshot(market(), feed(), now=301, wall_time=EPOCH + 401)
        self.assertEqual(shifted["status"], "unavailable")
        outlook.observe(quote(), now=320, wall_time=EPOCH + 320)
        result = snapshot(outlook, now=320)
        self.assertEqual(result["status"], "warming_up")
        self.assertIsNone(result["up_percent"])

    def test_wall_clock_jumps_reanchor_and_recover_after_fresh_warmup(self):
        for correction in (-100, 100):
            with self.subTest(correction=correction):
                outlook = warm()
                self.assertTrue(snapshot(outlook)["history"])
                outlook.observe(quote(), now=301, wall_time=EPOCH + 301 + correction)
                warming = outlook.snapshot(market(), feed(), now=301,
                                            wall_time=EPOCH + 301 + correction)
                self.assertEqual(warming["status"], "warming_up")
                self.assertEqual(warming["history"], [])
                for second in range(302, 362):
                    outlook.observe(quote(), now=second,
                                    wall_time=EPOCH + second + correction)
                recovered = outlook.snapshot(market(), feed(), now=361,
                                              wall_time=EPOCH + 361 + correction)
                self.assertEqual(recovered["status"], "live")
                self.assertEqual(recovered["history"], [{"elapsed_seconds": 361 + correction,
                                                         "up_percent": 50}])

    def test_disabled_is_explicit_and_json_safe(self):
        result = snapshot(warm(), enabled=False)
        self.assertFalse(result["enabled"])
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["up_percent"])
        json.dumps(result, allow_nan=False)

    def test_only_official_valid_settlement_can_show_zero_or_one_hundred(self):
        outlook = warm(1001000, end=899)
        expired = snapshot(outlook, 1001000, now=900)
        self.assertEqual(expired["status"], "awaiting_settlement")
        self.assertIsNone(expired["up_percent"])
        for status, expected in (("settled", 100), ("finalized", 0)):
            result = snapshot(outlook, now=901,
                              book=market(market_status=status,
                                          settlement_result="yes" if expected else "no"),
                              kraken={})
            self.assertEqual(result["status"], "settled")
            self.assertEqual(result["up_percent"], expected)
            self.assertEqual(result["up_percent"] + result["down_percent"], 100)
        for status in ("open", "closed", "finalized"):
            result = snapshot(outlook, now=901,
                              book=market(market_status=status, settlement_result="unknown"))
            self.assertEqual(result["status"], "awaiting_settlement")
            self.assertIsNone(result["up_percent"])
        spoofed = snapshot(outlook, book=market(market_status="open", settlement_result="yes"),
                           now=899, mid=1001000)
        self.assertEqual(spoofed["up_percent"], 95)
        stale_result = snapshot(outlook, now=901,
                                book=market(market_status="settled", settlement_result="yes", age_ms=3001))
        self.assertIsNone(stale_result["up_percent"])


class OutlookHistoryTests(unittest.TestCase):
    def test_reads_do_not_create_samples_or_change_forecast(self):
        outlook = warm(100020)
        original = snapshot(outlook, 100020)
        for index in range(10):
            current = snapshot(outlook, 100020, now=300 + index / 10)
            self.assertEqual(current["history"], original["history"])
            self.assertEqual(current["up_percent"], original["up_percent"])
            self.assertEqual(current["updated_at"], original["updated_at"])
        self.assertEqual(original["history"][0]["elapsed_seconds"], 300)
        self.assertEqual(len(original["history"]), 1)
        # External mutation of one response cannot corrupt the model's chart.
        original["history"][0]["up_percent"] = -1
        self.assertGreaterEqual(snapshot(outlook, 100020)["history"][0]["up_percent"], 5)

    def test_chart_retains_gaps_and_is_bounded_to_one_point_per_five_seconds(self):
        outlook = SettlementOutlook()
        for second in range(900):
            for fraction in (0, 0.2, 0.4, 0.6, 0.8):
                now = second + fraction
                outlook.observe(quote(), now=now, wall_time=EPOCH + now)
                result = snapshot(outlook, now=now)
        self.assertEqual(result["status"], "live")
        self.assertLessEqual(len(outlook._quotes), 360)
        self.assertLessEqual(len(result["history"]), 181)
        buckets = [math.floor(point["elapsed_seconds"] / 5) for point in result["history"]]
        self.assertEqual(len(buckets), len(set(buckets)))
        self.assertGreaterEqual(result["history"][0]["elapsed_seconds"], 60)
        preserved = copy.deepcopy(result["history"])
        self.assertEqual(snapshot(outlook, now=903)["history"], preserved)

    def test_rollover_and_floor_revision_clear_chart_but_preserve_spot_warmup(self):
        outlook = warm(end=899)
        before = snapshot(outlook, now=899)
        self.assertEqual(before["history"][0]["elapsed_seconds"], 899)
        outlook.observe(quote(), now=901, wall_time=EPOCH + 901)
        next_book = market(ticker="KXBTC15M-NEXT", close_time=iso(EPOCH + 1800))
        rolled = snapshot(outlook, now=901, book=next_book)
        self.assertEqual(rolled["status"], "live")
        self.assertEqual(rolled["history"], [{"elapsed_seconds": 1, "up_percent": 50}])
        revised = snapshot(outlook, now=901, book={**next_book, "strike_price": 100100})
        self.assertEqual(len(revised["history"]), 1)
        self.assertEqual(revised["history"][0]["elapsed_seconds"], 1)
        self.assertNotEqual(revised["up_percent"], before["up_percent"])


if __name__ == "__main__":
    unittest.main()
