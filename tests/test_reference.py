import unittest

from feedboard.reference import normalize_reference


class ReferenceTests(unittest.TestCase):
    def market(self, **changes):
        return {
            "ticker": "KXBTC15M-26OCT182315-15",
            "market_type": "binary",
            "strike_type": "greater_or_equal",
            "floor_strike": 82604.55,
            **changes,
        }

    def test_uses_official_floor_for_supported_yes_above_contract(self):
        for strike_type in ("greater", "greater_or_equal"):
            with self.subTest(strike_type=strike_type):
                self.assertEqual(
                    normalize_reference(self.market(strike_type=strike_type)),
                    {"strike_price": 82604.55, "strike_source": "kalshi.floor_strike"},
                )

    def test_accepts_numeric_string_without_parsing_presentation_text(self):
        self.assertEqual(normalize_reference(self.market(floor_strike="82604.55"))["strike_price"],
                         82604.55)
        self.assertIsNone(normalize_reference(self.market(floor_strike="$82,604.55"))["strike_price"])

    def test_rejects_missing_invalid_nonfinite_or_nonpositive_strike(self):
        for value in (None, True, False, "TBD", [], {}, 0, -1, float("inf"),
                      float("nan"), "NaN", "Infinity"):
            with self.subTest(value=value):
                self.assertEqual(normalize_reference(self.market(floor_strike=value)),
                                 {"strike_price": None, "strike_source": None})

    def test_requires_supported_contract_and_unambiguous_yes_above_semantics(self):
        for changes in ({"ticker": "OTHER-26OCT18"}, {"ticker": None},
                        {"market_type": None}, {"market_type": "scalar"},
                        {"strike_type": None}, {"strike_type": "less"},
                        {"strike_type": "between"}, {"strike_type": "custom"},
                        {"cap_strike": 83000}):
            with self.subTest(changes=changes):
                self.assertIsNone(normalize_reference(self.market(**changes))["strike_price"])

    def test_does_not_invent_reference_from_text_or_custom_metadata(self):
        result = normalize_reference(self.market(
            floor_strike=None,
            yes_sub_title="Target Price: $82,604.55",
            custom_strike={"round_digits": "2", "price": 82604.55},
            title="Bitcoin above $82,604.55?",
            kraken_start_price=82604.55,
        ))
        self.assertEqual(result, {"strike_price": None, "strike_source": None})


if __name__ == "__main__":
    unittest.main()
