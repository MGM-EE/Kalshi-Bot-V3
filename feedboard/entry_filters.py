"""Bounded, in-memory entry checks. Observation happens only in engine steps."""
from collections import deque
import math

TREND_SECONDS = 60


def entry_requirements(config, signal, side, context):
    """Entry-local thresholds; never rewrite the signal used by risk exits."""
    factor = 1.
    if (config.range_bias_enabled and context.get("ready")
            and context.get("regime") == "range" and side in ("up", "down")):
        favored = context.get("favored")
        if favored in ("up", "down"):
            factor += -config.range_bias_strength if favored == side else config.range_bias_strength
        elif context.get("position") is not None and .2 < context["position"] < .8:
            factor += config.range_bias_strength / 2
    return {"momentum5_bps": signal["floor5"] * factor,
            "momentum20_bps": signal["floor20"] * factor,
            "flow_imbalance": min(1., config.flow_imbalance * factor), "range_factor": factor}


def in_entry_window(config, close, wall_time):
    if not config.entry_window_enabled:
        return True
    if close is None:
        return False
    elapsed = wall_time - (close - 900)
    return config.entry_start_minute * 60 <= elapsed < config.entry_end_minute * 60


class EntryQuality:
    def __init__(self):
        self.points = deque(maxlen=66)
        self.last_step = self.last_wall = self.last_quote = None
        self.quote_token = None
        self.checked = False
        self.clear_confirmation()

    def clear_confirmation(self):
        self.key = self.since = self.first_quote = self.first_book = None
        self.new_quote = self.new_book = False

    def observe(self, midpoint, token, now, wall_time):
        self.checked = False
        if self.last_step is not None:
            delta = now - self.last_step
            if delta < 0 or delta > 3 or abs(wall_time - self.last_wall - delta) > 3:
                self.points.clear()
                self.quote_token = self.last_quote = None
                self.clear_confirmation()
            elif delta > 1:
                self.clear_confirmation()
        self.last_step, self.last_wall = now, wall_time
        if midpoint is None:
            self.points.clear()
            self.quote_token = self.last_quote = None
            self.clear_confirmation()
            return
        if self.last_quote is not None and now - self.last_quote > 3:
            self.points.clear()
            self.clear_confirmation()
            self.quote_token = None
        if token != self.quote_token:
            point = (now, midpoint)
            if self.points and math.floor(self.points[-1][0]) == math.floor(now):
                self.points[-1] = point
            else:
                self.points.append(point)
            self.quote_token, self.last_quote = token, now
        while self.points and now - self.points[0][0] > TREND_SECONDS + 2:
            self.points.popleft()

    def trend(self):
        if not self.points:
            return {"efficiency": None, "signed_efficiency": None, "net_change_bps": None,
                    "coverage_seconds": 0, "points": 0}
        now = self.last_step
        anchor = next((i for i in range(len(self.points) - 1, -1, -1)
                       if self.points[i][0] <= now - TREND_SECONDS), None)
        coverage = max(0., now - self.points[0][0])
        if anchor is None:
            return {"efficiency": None, "signed_efficiency": None, "net_change_bps": None,
                    "coverage_seconds": coverage, "points": len(self.points)}
        points = list(self.points)[anchor:]
        movement = sum(abs(b[1] - a[1]) for a, b in zip(points, points[1:]))
        change = points[-1][1] - points[0][1]
        signed = max(-1., min(1., change / movement)) if movement else 0.
        change_bps = change / points[0][1] * 10000
        return {"efficiency": abs(signed), "signed_efficiency": signed,
                "net_change_bps": change_bps if math.isfinite(change_bps) else None,
                "coverage_seconds": coverage, "points": len(points)}

    def confirm(self, key, now, quote_token, book_version, seconds):
        self.checked = True
        if key != self.key:
            self.clear_confirmation()
            self.key, self.since = key, now
            self.first_quote, self.first_book = quote_token, book_version
        self.new_quote |= quote_token != self.first_quote
        self.new_book |= book_version != self.first_book
        return now - self.since >= seconds and self.new_quote and self.new_book

    def view(self):
        return {**self.trend(), "lookback_seconds": TREND_SECONDS,
                "confirmation_side": self.key[1] if self.key else None,
                "confirmation_ticker": self.key[0] if self.key else None,
                "confirmation_elapsed_seconds": max(0., self.last_step - self.since) if self.since is not None else 0.,
                "confirmation_new_quote": self.new_quote, "confirmation_new_book": self.new_book}
