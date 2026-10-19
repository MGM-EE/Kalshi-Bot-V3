"""Offline normalization and state tests; all prices here are synthetic."""

import os
import unittest
from email.utils import formatdate
from unittest.mock import patch

from feedboard.core import (
    Settings,
    Store,
    kalshi_price,
    normalize_kalshi,
    normalize_kraken_rest,
    normalize_kraken_ticker,
    number,
    retry_after_seconds,
    websocket_proxy,
)


def market(ticker="TEST-MARKET", **values):
    return {"ticker": ticker, "title": "Synthetic test market", **values}


def ticker(last=65000, **values):
    return {
        "channel": "ticker",
        "type": "update",
        "data": [{"symbol": "BTC/USD", "bid": "64999.5", "ask": 65000.5,
                  "last": last, **values}],
    }


def rest_ticker(**values):
    return {"error": [], "result": {"XXBTZUSD": {
        "b": ["64999.5", "1", "1.0"],
        "a": ["65000.5", "1", "1.0"],
        "c": ["65000.0", "0.1"], **values,
    }}}


class WebSocketProxyTests(unittest.TestCase):
    def test_secure_websocket_uses_inherited_https_proxy(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://synthetic-proxy:8080"}, clear=True):
            self.assertEqual(websocket_proxy("wss://ws.kraken.com/v2"),
                             "http://synthetic-proxy:8080")

    def test_explicit_websocket_proxy_takes_precedence(self):
        with patch.dict(os.environ, {"https_proxy": "http://synthetic-https:8080",
                                     "wss_proxy": "http://synthetic-wss:8080"}, clear=True):
            self.assertEqual(websocket_proxy("wss://ws.kraken.com/v2"),
                             "http://synthetic-wss:8080")

    def test_plain_websocket_uses_http_proxy(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://synthetic-http:8080"}, clear=True):
            self.assertEqual(websocket_proxy("ws://example.test/socket"),
                             "http://synthetic-http:8080")

    def test_no_proxy_bypasses_loopback_and_selected_exchange(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://synthetic-proxy:8080",
                                     "NO_PROXY": "127.0.0.1,localhost,ws.kraken.com"}, clear=True):
            for url in ("wss://127.0.0.1:1234/feed", "wss://localhost/feed",
                        "wss://ws.kraken.com/v2"):
                with self.subTest(url=url):
                    self.assertIsNone(websocket_proxy(url))

    def test_unconfigured_proxy_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(websocket_proxy("wss://ws.kraken.com/v2"))


class RetryAfterTests(unittest.TestCase):
    def test_large_numeric_delay_is_preserved(self):
        self.assertEqual(retry_after_seconds("300"), 300)
        self.assertEqual(retry_after_seconds("0.5"), 0.5)

    def test_http_date_uses_remaining_wall_clock_delay(self):
        now = 1700000000
        with patch("feedboard.core.time.time", return_value=now):
            self.assertEqual(retry_after_seconds(formatdate(now + 120, usegmt=True)), 120)
            self.assertEqual(retry_after_seconds(formatdate(now - 120, usegmt=True)), 0)

    def test_missing_invalid_or_nonfinite_header_uses_default(self):
        for value in (None, "", "bad-date", "NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                self.assertEqual(retry_after_seconds(value), 5)

    def test_negative_numeric_delay_does_not_produce_negative_sleep(self):
        self.assertEqual(retry_after_seconds("-1"), 0)


class KalshiNormalizationTests(unittest.TestCase):
    def test_fixed_point_dollars_take_precedence_over_legacy_cents(self):
        self.assertEqual(kalshi_price({"yes_bid_dollars": "0.123456", "yes_bid": 99},
                                      "yes_bid"), 0.123456)
        self.assertEqual(kalshi_price({"yes_bid": 42}, "yes_bid"), 0.42)

    def test_missing_prices_stay_missing_and_real_zero_is_valid(self):
        self.assertIsNone(kalshi_price({}, "yes_bid"))
        self.assertEqual(kalshi_price({"yes_bid_dollars": "0.000000"}, "yes_bid"), 0)
        self.assertEqual(kalshi_price({"yes_bid_dollars": "1.000000"}, "yes_bid"), 1)

    def test_invalid_current_field_does_not_fall_back_to_legacy_quote(self):
        for value in (None, "", "bad", "NaN", "Infinity", -0.01, 1.01, True, [], {}):
            with self.subTest(value=value):
                self.assertIsNone(kalshi_price({"yes_bid_dollars": value, "yes_bid": 50},
                                               "yes_bid"))

    def test_legacy_cents_are_validated_after_conversion(self):
        for value in (-1, 101, True, "NaN", None):
            with self.subTest(value=value):
                self.assertIsNone(kalshi_price({"yes_bid": value}, "yes_bid"))

    def test_market_list_is_bounded_and_preserves_missing_quotes(self):
        rows = [market(f"TEST-{index}", yes_bid_dollars="0.12") for index in range(12)]
        normalized = normalize_kalshi({"markets": rows}, 3)
        self.assertEqual([row["ticker"] for row in normalized], ["TEST-0", "TEST-1", "TEST-2"])
        self.assertEqual(normalized[0]["yes_bid"], 0.12)
        self.assertIsNone(normalized[0]["yes_ask"])
        self.assertIsNone(normalized[0]["last"])

    def test_invalid_market_response_is_rejected(self):
        for payload in (None, [], {}, {"markets": None}, {"markets": {}},
                        {"markets": [None]}, {"markets": [{"ticker": 12}]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                normalize_kalshi(payload, 3)


class KrakenNormalizationTests(unittest.TestCase):
    def test_ticker_accepts_numeric_strings_and_numbers(self):
        self.assertEqual(normalize_kraken_ticker(ticker("65000.25"), "BTC/USD"), {
            "symbol": "BTC/USD", "bid": 64999.5, "ask": 65000.5, "last": 65000.25,
        })

    def test_heartbeats_acknowledgments_and_other_pairs_are_ignored(self):
        for payload in ({"channel": "heartbeat"}, {"method": "subscribe", "success": True},
                        ticker(symbol="ETH/USD"), None, []):
            with self.subTest(payload=payload):
                self.assertIsNone(normalize_kraken_ticker(payload, "BTC/USD"))

    def test_matching_row_can_follow_an_unrelated_pair(self):
        payload = ticker()
        payload["data"].insert(0, {"symbol": "ETH/USD", "last": 1000})
        self.assertEqual(normalize_kraken_ticker(payload, "BTC/USD")["last"], 65000)

    def test_missing_nonfinite_and_nonpositive_ticker_prices_are_rejected(self):
        for field in ("bid", "ask", "last"):
            for value in (None, "NaN", "Infinity", 0, -1, True, "bad"):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    normalize_kraken_ticker(ticker(**{field: value}), "BTC/USD")
        with self.assertRaises(ValueError):
            normalize_kraken_ticker({"channel": "ticker", "data": {}}, "BTC/USD")

    def test_rest_maps_legacy_pair_response_to_requested_symbol(self):
        self.assertEqual(normalize_kraken_rest(rest_ticker(), "BTC/USD"), {
            "symbol": "BTC/USD", "bid": 64999.5, "ask": 65000.5, "last": 65000,
        })

    def test_rest_errors_missing_pairs_and_invalid_prices_are_rejected(self):
        payloads = [None, {}, {"error": ["EGeneral:Unavailable"]}, {"error": [], "result": {}},
                    {"result": {"A": {}, "B": {}}}, rest_ticker(b=[]),
                    rest_ticker(a=["NaN"]), rest_ticker(c=[0]), rest_ticker(b=None)]
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                normalize_kraken_rest(payload, "BTC/USD")

    def test_rest_rejects_string_in_place_of_price_array(self):
        with self.assertRaises(ValueError):
            normalize_kraken_rest(rest_ticker(b="64999.5"), "BTC/USD")

    def test_number_rejects_values_that_cannot_be_serialized_as_finite_prices(self):
        for value in (True, False, None, float("nan"), float("inf"), "-Infinity", {}, []):
            with self.subTest(value=value):
                self.assertIsNone(number(value))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(Settings())

    def make_ready(self):
        self.store.update("kalshi", {"series": "TEST", "markets": [market()]}, "rest", 12.345)
        self.store.update("kraken", {"symbol": "BTC/USD", "last": 65000}, "websocket")

    def test_readiness_requires_both_fresh_feeds_and_an_open_market(self):
        self.assertFalse(self.store.ready())
        self.store.update("kraken", {"symbol": "BTC/USD", "last": 65000}, "websocket")
        self.assertFalse(self.store.ready())
        self.store.update("kalshi", {"series": "TEST", "markets": []}, "rest")
        self.assertFalse(self.store.ready())
        self.store.update("kalshi", {"series": "TEST", "markets": [market()]}, "rest")
        self.assertTrue(self.store.ready())

    def test_failure_retains_last_sample_and_recovery_clears_error(self):
        self.make_ready()
        original = self.store.snapshot()["feeds"]["kalshi"]
        with self.assertLogs("feedboard.core", level="WARNING"):
            self.store.fail("kalshi", ConnectionError("synthetic failure"))
        failed = self.store.snapshot()["feeds"]["kalshi"]
        self.assertEqual(failed["data"], original["data"])
        self.assertEqual(failed["received_at"], original["received_at"])
        self.assertEqual(failed["updates"], 1)
        self.assertEqual(failed["status"], "retrying")
        self.assertIn("synthetic failure", failed["error"])
        self.assertFalse(self.store.ready())
        self.store.update("kalshi", original["data"], "rest")
        self.assertIsNone(self.store.snapshot()["feeds"]["kalshi"]["error"])
        self.assertTrue(self.store.ready())

    def test_stale_sample_is_reported_without_altering_its_value(self):
        self.make_ready()
        received = self.store.feeds["kalshi"].received_mono
        with patch("feedboard.core.time.monotonic", return_value=received + 16):
            snapshot = self.store.snapshot()
            self.assertEqual(snapshot["feeds"]["kalshi"]["status"], "stale")
            self.assertEqual(snapshot["feeds"]["kalshi"]["age_ms"], 16000)
            self.assertEqual(snapshot["feeds"]["kalshi"]["data"]["markets"][0]["ticker"],
                             "TEST-MARKET")
            self.assertFalse(self.store.ready())

    def test_burst_keeps_only_latest_quote_and_bounded_loop_metrics(self):
        for index in range(1000):
            self.store.update("kraken", {"symbol": "BTC/USD", "last": 65000 + index}, "websocket")
            self.store.loop_lags.append(float(index))
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["feeds"]["kraken"]["data"]["last"], 65999)
        self.assertEqual(snapshot["feeds"]["kraken"]["updates"], 1000)
        self.assertEqual(len(self.store.loop_lags), 300)
        self.assertEqual(snapshot["metrics"]["loop_lag_ms"], 984)

    def test_settings_reject_unbounded_or_nonpositive_configuration(self):
        for values in ({"market_limit": 0}, {"market_limit": 6}, {"poll_seconds": 0.1},
                       {"request_timeout": 0}, {"retry_base": 0}, {"retry_cap": 0.1},
                       {"stale_seconds": 0}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Settings(**values)

    def test_settings_reject_nonfinite_timing_values(self):
        for field in ("poll_seconds", "request_timeout", "stale_seconds", "retry_base", "retry_cap"):
            for value in (float("nan"), float("inf"), -float("inf")):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    Settings(**{field: value})


if __name__ == "__main__":
    unittest.main()
