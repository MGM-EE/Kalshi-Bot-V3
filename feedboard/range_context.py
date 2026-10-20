"""Causal rolling price-range context, not a calibrated reversal prediction.

Bounds are p10/p90 of completed observed five-second buckets over thirty minutes.
The current bucket never sets its own support/resistance. Touch evidence ages out
with the history and resets when the band moves materially. Breakout confirmation
uses frozen thresholds; recovery requires sustained return to the updated band.
All observation is in memory, bounded, and independent of exchange requests.
"""
from collections import deque
import math

BUCKET_SECONDS = 5
LOOKBACK_SECONDS = 1800
WARMUP_SECONDS = 600
MIN_POINTS = 100
MIN_WIDTH_FRACTION = .0005
TOUCH_SPACING_SECONDS = 60
BREAKOUT_SECONDS = 10
MODEL_ID = "rolling-percentile-range-v1"


def _finite(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _percentile(values, fraction):
    offset = (len(values) - 1) * fraction
    low = math.floor(offset)
    high = math.ceil(offset)
    return values[low] + (values[high] - values[low]) * (offset - low)


class RollingRange:
    """Observe valid BTC midpoints only; ``None`` marks unavailable input.

    A band is ready after ten minutes and 100 completed buckets. It gains a
    favored direction only at a twice-tested edge. Outside the band, and during
    either pending or confirmed breakouts, the range contributes no direction.
    Calling ``view`` does not advance clocks, invent samples, or age evidence.
    """
    def __init__(self):
        self.last_now = self.last_wall = None
        self._reset_history()

    def _reset_history(self):
        self.points = deque(maxlen=363)
        self.last_quote = self.token = None
        self.midpoint = None
        self.fresh = False
        self.cache_bucket = None
        self.lower = self.upper = None
        self.covered_seconds = 0.
        self.completed_points = 0
        self.band_ready = False
        self.status = "unavailable"
        self.touch_reference = None
        self.touches = {"lower": deque(maxlen=31), "upper": deque(maxlen=31)}
        self.touch_active = {"lower": False, "upper": False}
        self.touch_anchor = {"lower": None, "upper": None}
        self._clear_breakout()

    def _clear_breakout(self):
        self.break_side = self.break_since = self.break_bucket = None
        self.break_lower = self.break_upper = None
        self.break_buckets = 0
        self.break_confirmed = False
        self.recovery_since = self.recovery_bucket = None
        self.recovery_buckets = 0

    def _reset_touches(self):
        for side in self.touches:
            self.touches[side].clear()
            self.touch_active[side] = False
            self.touch_anchor[side] = None
        self.touch_reference = (self.lower, self.upper) if self.band_ready else None

    def _refresh(self, now, bucket):
        if bucket == self.cache_bucket:
            return
        self.cache_bucket = bucket
        completed = [point for point in self.points if point[0] < bucket]
        self.completed_points = len(completed)
        self.covered_seconds = max(0., now - completed[0][1]) if completed else 0.
        self.band_ready = False
        if not completed:
            self.lower = self.upper = None
            self.status = "warming_up"
            return
        values = sorted(point[2] for point in completed)
        self.lower, self.upper = _percentile(values, .1), _percentile(values, .9)
        if self.covered_seconds < WARMUP_SECONDS or len(completed) < MIN_POINTS:
            self.status = "warming_up"
            return
        middle = self.lower + (self.upper - self.lower) / 2
        if self.upper - self.lower < MIN_WIDTH_FRACTION * middle:
            self.status = "flat"
            self._reset_touches()
            self._clear_breakout()
            return
        self.band_ready, self.status = True, "ready"
        if self.touch_reference is None:
            self._reset_touches()
        else:
            old_lower, old_upper = self.touch_reference
            tolerance = (old_upper - old_lower) * .2
            if abs(self.lower - old_lower) > tolerance or abs(self.upper - old_upper) > tolerance:
                self._reset_touches()

    def _touch(self, now, position):
        # Hysteresis prevents one noisy visit from producing many touches.
        # Require an actual price retreat as well, so moving percentiles alone
        # cannot turn an unchanged price into a fresh independent test.
        lower_anchor = self.touch_anchor["lower"]
        upper_anchor = self.touch_anchor["upper"]
        if position > .3 and (lower_anchor is None or self.midpoint - lower_anchor[0] >= .1 * lower_anchor[1]):
            self.touch_active["lower"] = False
        if position < .7 and (upper_anchor is None or upper_anchor[0] - self.midpoint >= .1 * upper_anchor[1]):
            self.touch_active["upper"] = False
        for side, inside in (("lower", -.15 <= position <= .2),
                             ("upper", .8 <= position <= 1.15)):
            times = self.touches[side]
            if inside and not self.touch_active[side]:
                if not times or now - times[-1] >= TOUCH_SPACING_SECONDS:
                    times.append(now)
                self.touch_active[side] = True
                self.touch_anchor[side] = (self.midpoint, self.upper - self.lower)

    def _breakout(self, now, bucket):
        price = self.midpoint
        if self.break_confirmed:
            # A new sideways band can become established after a breakout.
            # Rejoining it requires sustained real observations, not one touch.
            if self.lower <= price <= self.upper:
                if self.recovery_since is None:
                    self.recovery_since, self.recovery_bucket = now, bucket
                    self.recovery_buckets = 1
                elif bucket != self.recovery_bucket:
                    self.recovery_bucket = bucket
                    self.recovery_buckets += 1
                if now - self.recovery_since >= BREAKOUT_SECONDS and self.recovery_buckets >= 3:
                    self._clear_breakout()
                    self._reset_touches()
            else:
                self.recovery_since = self.recovery_bucket = None
                self.recovery_buckets = 0
            return
        lower = self.break_lower if self.break_side else self.lower
        upper = self.break_upper if self.break_side else self.upper
        width = upper - lower
        side = "up" if price > upper + .15 * width else "down" if price < lower - .15 * width else None
        if side is None:
            self._clear_breakout()
            return
        if side != self.break_side:
            self._clear_breakout()
            self.break_side, self.break_since, self.break_bucket = side, now, bucket
            self.break_lower, self.break_upper = self.lower, self.upper
            self.break_buckets = 1
        elif bucket != self.break_bucket:
            self.break_bucket = bucket
            self.break_buckets += 1
        if now - self.break_since >= BREAKOUT_SECONDS and self.break_buckets >= 3:
            self.break_confirmed = True

    def observe(self, midpoint, token, now, wall_time):
        if not _finite(now) or not _finite(wall_time):
            raise ValueError("Range observation clocks must be finite numbers")
        if self.last_now is not None:
            elapsed = now - self.last_now
            if elapsed < 0 or elapsed > 15 or abs(wall_time - self.last_wall - elapsed) > 3:
                self._reset_history()
        if self.last_quote is not None and now - self.last_quote > 15:
            self._reset_history()
        self.last_now, self.last_wall = now, wall_time
        while self.points and now - self.points[0][1] > LOOKBACK_SECONDS:
            self.points.popleft()
        for times in self.touches.values():
            while times and now - times[0] > LOOKBACK_SECONDS:
                times.popleft()
        self.fresh = _finite(midpoint) and midpoint > 0
        if not self.fresh:
            self.midpoint = None
            if not self.break_confirmed:
                self._clear_breakout()
            self.recovery_since = self.recovery_bucket = None
            self.recovery_buckets = 0
            return
        self.midpoint = float(midpoint)
        bucket = math.floor(now / BUCKET_SECONDS)
        changed = token != self.token
        if changed:
            point = (bucket, now, self.midpoint)
            if self.points and self.points[-1][0] == bucket:
                self.points[-1] = point
            else:
                self.points.append(point)
            self.token, self.last_quote = token, now
        self._refresh(now, bucket)
        if self.band_ready and changed:
            self._breakout(now, bucket)
            if self.break_side is None:
                self._touch(now, (self.midpoint - self.lower) / (self.upper - self.lower))

    def view(self):
        ready = self.fresh and self.band_ready
        position = (max(-1e6, min(1e6, (self.midpoint - self.lower) / (self.upper - self.lower)))
                    if ready else None)
        regime, favored = "unavailable", None
        if ready:
            if self.break_side:
                prefix = "breakout_" if self.break_confirmed else "pending_breakout_"
                regime = prefix + self.break_side
            elif not 0 <= position <= 1:
                regime = "outside_range"
            else:
                regime = "range"
                if position <= .2 and len(self.touches["lower"]) >= 2:
                    favored = "up"
                elif position >= .8 and len(self.touches["upper"]) >= 2:
                    favored = "down"
        return {"model_id": MODEL_ID, "ready": ready,
                "status": self.status if self.fresh else "unavailable",
                "lower": self.lower, "upper": self.upper, "position": position,
                "lower_touches": len(self.touches["lower"]), "upper_touches": len(self.touches["upper"]),
                "favored": favored, "regime": regime,
                "covered_seconds": self.covered_seconds, "points": len(self.points),
                "completed_points": self.completed_points,
                "pending_breakout": self.break_side is not None and not self.break_confirmed,
                "breakout_seconds": max(0., self.last_now - self.break_since) if self.break_since is not None else 0.,
                "breakout_lower": self.break_lower, "breakout_upper": self.break_upper}
