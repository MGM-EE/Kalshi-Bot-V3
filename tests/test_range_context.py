"""Deterministic rolling-range evidence; no exchange calls or fitted profits."""
import copy
import json
import unittest

from feedboard.range_context import RollingRange

EPOCH = 1_800_000_000.


def observe(model, now, price, token=None, wall=None):
    model.observe(price, now if token is None else token, now, EPOCH + now if wall is None else wall)


def warmed():
    model = RollingRange()
    # Repeated sweeps provide a wide, nonflat range, without future information.
    for now in range(0, 605, 5):
        observe(model, now, 100 + ((now // 5) % 20))
    assert model.view()["ready"]
    return model


class RangeTests(unittest.TestCase):
    def test_warmup_requires_time_and_real_completed_buckets(self):
        model = RollingRange()
        self.assertFalse(model.view()["ready"])
        for now in range(0, 600, 5):
            observe(model, now, 100 + (now // 5) % 20)
            self.assertFalse(model.view()["ready"])
        observe(model, 600, 105)
        self.assertTrue(model.view()["ready"])
        sparse = RollingRange()
        for now in range(0, 610, 10):
            observe(sparse, now, 100 + (now // 10) % 20)
        self.assertFalse(sparse.view()["ready"])
        self.assertLess(sparse.view()["completed_points"], 100)

    def test_current_bucket_cannot_move_its_own_bounds_and_one_outlier_is_robust(self):
        model = warmed()
        before = model.view()
        observe(model, 600.1, 10000)
        self.assertEqual(model.view()["lower"], before["lower"])
        self.assertEqual(model.view()["upper"], before["upper"])
        self.assertEqual(model.view()["regime"], "pending_breakout_up")
        observe(model, 605, 110)
        self.assertLess(abs(model.view()["upper"] - before["upper"]), 1)
        self.assertIsNone(model.view()["favored"])

    def test_latest_quote_per_bucket_and_read_only_views(self):
        model = warmed()
        before = model.view()
        count = len(model.points)
        observe(model, 601, 109)
        self.assertEqual(len(model.points), count)
        self.assertEqual(model.points[-1][2], 109)
        self.assertEqual(model.view()["upper"], before["upper"])
        state = copy.deepcopy(model.__dict__)
        for _ in range(25):
            json.dumps(model.view(), allow_nan=False)
        self.assertEqual(model.__dict__, state)

    def test_two_independent_lower_touches_need_spacing_and_hysteresis(self):
        model = warmed()
        low, high = model.view()["lower"], model.view()["upper"]
        middle = (low + high) / 2
        observe(model, 601, low + .05 * (high-low))
        self.assertEqual(model.view()["lower_touches"], 1)
        self.assertIsNone(model.view()["favored"])
        # Hovering at the boundary is a single touch, including after a minute.
        for now in range(605, 670, 5):
            observe(model, now, low + .05 * (high-low))
        self.assertEqual(model.view()["lower_touches"], 1)
        observe(model, 670, middle)
        observe(model, 675, model.view()["lower"] + .05 * (model.view()["upper"]-model.view()["lower"]))
        self.assertEqual(model.view()["lower_touches"], 2)
        self.assertEqual(model.view()["favored"], "up")

    def test_rapid_retests_do_not_count_as_independent_upper_touches(self):
        model = warmed()
        low, high = model.view()["lower"], model.view()["upper"]
        observe(model, 601, high - .05 * (high-low))
        self.assertEqual(model.view()["upper_touches"], 1)
        observe(model, 602, (low + high) / 2)
        observe(model, 603, high - .05 * (high-low))
        self.assertEqual(model.view()["upper_touches"], 1)
        for now in range(605, 670, 5):
            observe(model, now, (model.view()["lower"] + model.view()["upper"]) / 2)
        observe(model, 670, model.view()["upper"] - .05 * (model.view()["upper"]-model.view()["lower"]))
        self.assertEqual(model.view()["upper_touches"], 2)
        self.assertEqual(model.view()["favored"], "down")

    def test_pending_and_confirmed_breakouts_disable_bias_and_freeze_thresholds(self):
        for direction in (1, -1):
            model = warmed()
            lower, upper = model.view()["lower"], model.view()["upper"]
            outside = upper + (upper-lower) if direction == 1 else lower - (upper-lower)
            side = "up" if direction == 1 else "down"
            observe(model, 601, outside)
            self.assertEqual(model.view()["regime"], "pending_breakout_"+side)
            frozen = (model.view()["breakout_lower"], model.view()["breakout_upper"])
            observe(model, 606, outside)
            self.assertFalse(model.break_confirmed)
            observe(model, 611, outside)
            self.assertEqual(model.view()["regime"], "breakout_"+side)
            self.assertIsNone(model.view()["favored"])
            self.assertEqual((model.view()["breakout_lower"], model.view()["breakout_upper"]), frozen)
            # Recovery is itself sustained, rather than a one-update reentry.
            for now in (612, 617):
                observe(model, now, (model.view()["lower"]+model.view()["upper"])/2)
                self.assertEqual(model.view()["regime"], "breakout_"+side)
            observe(model, 622, (model.view()["lower"]+model.view()["upper"])/2)
            self.assertEqual(model.view()["regime"], "range")
            self.assertEqual(model.view()["lower_touches"], 0)
            self.assertEqual(model.view()["upper_touches"], 0)

    def test_breakout_requires_distinct_observed_buckets_and_continuous_crossing(self):
        model = warmed()
        outside = model.view()["upper"] + 20
        observe(model, 601, outside, token="fixed")
        observe(model, 606, outside, token="fixed")
        observe(model, 611, outside, token="fixed")
        self.assertFalse(model.break_confirmed)
        observe(model, 612, (model.view()["lower"]+model.view()["upper"])/2)
        self.assertFalse(model.view()["pending_breakout"])
        observe(model, 613, outside)
        self.assertEqual(model.break_since, 613)

    def test_outside_prices_are_neutral_even_without_a_confirmed_breakout(self):
        model = warmed()
        lower, upper = model.view()["lower"], model.view()["upper"]
        observe(model, 601, upper + .05 * (upper-lower))
        self.assertEqual(model.view()["regime"], "outside_range")
        self.assertIsNone(model.view()["favored"])

    def test_invalid_data_short_gaps_and_long_gaps_or_clock_changes(self):
        model = warmed()
        count = len(model.points)
        observe(model, 601, None)
        self.assertEqual(model.view()["status"], "unavailable")
        self.assertFalse(model.view()["ready"])
        self.assertEqual(len(model.points), count)
        observe(model, 605, 110)
        self.assertTrue(model.view()["ready"])
        for kind in ("quote_gap", "step_gap", "reverse", "wall"):
            with self.subTest(kind=kind):
                model = warmed()
                if kind == "quote_gap":
                    for now in range(601, 617):observe(model, now, None)
                    observe(model, 617, 110)
                elif kind == "step_gap":observe(model, 616, 110)
                elif kind == "reverse":observe(model, 599, 110)
                else:observe(model, 601, 110, wall=EPOCH+610)
                self.assertFalse(model.view()["ready"])
                self.assertLessEqual(len(model.points), 1)

    def test_flat_and_too_narrow_ranges_never_gain_a_bias(self):
        for price in (lambda n:100, lambda n:100+(n%2)*.001):
            model = RollingRange()
            for now in range(0, 610, 5):observe(model, now, price(now))
            self.assertEqual(model.view()["status"], "flat")
            self.assertFalse(model.view()["ready"])
            self.assertIsNone(model.view()["favored"])

    def test_bounded_history_and_finite_invalid_input_handling(self):
        model = RollingRange()
        for now in range(0, 20000):
            observe(model, now, 100+(now%100)/10)
        self.assertLessEqual(len(model.points), 363)
        self.assertLessEqual(model.view()["covered_seconds"], 1800)
        for invalid in (None, -1, 0, True, "100", float("inf"), float("nan"), 10**1000):
            observe(model, 20000, invalid)
            self.assertFalse(model.view()["ready"])
            json.dumps(model.view(),allow_nan=False)
        for invalid in (True, None, float("nan"), float("inf")):
            with self.assertRaises(ValueError):model.observe(100, 1, invalid, EPOCH)
        # A finite but unreasonable input still cannot produce nonfinite JSON.
        extreme = warmed()
        observe(extreme,601,1e308)
        json.dumps(extreme.view(),allow_nan=False)
        tiny = RollingRange()
        for now in range(0,605,5):
            observe(tiny,now,1e-300*(1+(now//5)%20/1000))
        self.assertTrue(tiny.view()["ready"])
        observe(tiny,601,1e308)
        self.assertEqual(tiny.view()["position"],1e6)
        json.dumps(tiny.view(),allow_nan=False)

    def test_boundary_drift_alone_does_not_rearm_touch_evidence(self):
        model = warmed()
        observe(model,601,100)
        self.assertEqual(model.view()["lower_touches"],1)
        self.assertTrue(model.touch_active["lower"])
        # Simulate a position change caused solely by rolling bounds moving.
        model._touch(662,.5)
        self.assertTrue(model.touch_active["lower"])
        model._touch(663,.1)
        self.assertEqual(model.view()["lower_touches"],1)

    def test_material_boundary_shift_discards_stale_touch_evidence(self):
        model = warmed()
        model.touches["lower"].extend((540, 600))
        model.touches["upper"].extend((540, 600))
        for now in range(605, 1005, 5):
            observe(model, now, 130+(now%20))
        self.assertTrue(model.view()["ready"])
        self.assertLess(model.view()["lower_touches"], 2)
        self.assertLess(model.view()["upper_touches"], 2)
