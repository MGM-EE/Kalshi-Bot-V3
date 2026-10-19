"""Synthetic causal execution and chronological selection; no exchange calls."""
import unittest
from dataclasses import replace

from feedboard.historical import Bar, Market
from feedboard.simulation import SimConfig, chronological_split, compare, run_config


def market(index=1, *, result="yes", quotes=((5, .29, .30), (6, .81, .82)), settled=None):
    opening = (index - 1) * 900
    return Market(f"KXBTC15M-TEST-{index}", opening + 900, settled or opening + 900,
                  result, 100000, tuple(Bar(opening + minute * 60, bid, ask) for minute, bid, ask in quotes))


class SimulationTests(unittest.TestCase):
    def test_buy_ask_sell_later_bid_slippage_and_rounded_fees(self):
        stats, trades = run_config(SimConfig(), [market()], details=True)
        trade = trades[0]
        self.assertEqual(trade["side"], "up")
        self.assertEqual(trade["entry_price"], .31)
        self.assertEqual(trade["exit_price"], .8)
        self.assertEqual(trade["entry_fee"], .08)
        self.assertEqual(trade["exit_fee"], .06)
        self.assertAlmostEqual(stats.net_pnl, 2.31)
        self.assertEqual(stats.target_exits, 1)

    def test_down_side_uses_complement_of_yes_quotes(self):
        stats, trades = run_config(SimConfig(), [market(result="no", quotes=((5, .70, .71), (6, .18, .19)))], details=True)
        self.assertEqual(trades[0]["side"], "down")
        self.assertEqual(trades[0]["entry_price"], .31)
        self.assertEqual(trades[0]["exit_price"], .8)
        self.assertAlmostEqual(stats.net_pnl, 2.31)

    def test_no_same_bar_exit_and_settlement_has_no_exit_fee(self):
        stats, trades = run_config(SimConfig(), [market(result="no", quotes=((5, .29, .30),))], details=True)
        self.assertEqual(trades[0]["exit_reason"], "settlement")
        self.assertEqual(trades[0]["exit_price"], 0)
        self.assertEqual(trades[0]["exit_fee"], 0)
        self.assertAlmostEqual(stats.net_pnl, -1.63)

    def test_target_requires_bid_after_slippage_to_clear_floor(self):
        stats, trades = run_config(SimConfig(), [market(result="no", quotes=((5, .29, .30), (6, .80, .81)))], details=True)
        self.assertEqual(stats.target_exits, 0)
        self.assertEqual(trades[0]["exit_reason"], "settlement")

    def test_entry_window_excludes_first_five_and_last_two_minutes(self):
        config = SimConfig(sell_target_cents=None)
        _, trades = run_config(config, [market(quotes=((4, .29, .30), (12, .29, .30), (13, .29, .30)))], details=True)
        self.assertEqual(trades[0]["entry_minute"], 12)
        stats, _ = run_config(config, [market(quotes=((4, .29, .30), (13, .29, .30)))])
        self.assertEqual(stats.trades, 0)

    def test_fill_price_cap_includes_slippage_and_spread_is_not_charged_twice(self):
        _, trades = run_config(SimConfig(), [market(quotes=((5, .39, .40), (6, .38, .39)))], details=True)
        self.assertEqual(trades[0]["entry_price"], .4)
        self.assertEqual(trades[0]["entry_minute"], 6)

    def test_missing_crossed_and_zero_asks_never_create_free_trades(self):
        for quotes in (((5, None, .3),), ((5, .31, .3),), ((5, 0, 0),), ((5, 1, 1),)):
            with self.subTest(quotes=quotes):
                stats, _ = run_config(SimConfig(buy_min_cents=1), [market(quotes=quotes)])
                self.assertEqual(stats.trades, 0)

    def test_entry_side_does_not_use_the_eventual_winner(self):
        config = SimConfig(sell_target_cents=None)
        _, first = run_config(config, [market(result="yes")], details=True)
        _, second = run_config(config, [market(result="no")], details=True)
        self.assertEqual(first[0]["side"], second[0]["side"])
        self.assertEqual(first[0]["entry_ts"], second[0]["entry_ts"])

    def test_cash_limits_and_realized_drawdown(self):
        config = SimConfig(sell_target_cents=None)
        stats, _ = run_config(config, [market(result="no"), market(2, result="no")], initial_cash=2)
        self.assertEqual(stats.trades, 1)
        self.assertEqual(stats.cash_blocked_markets, 1)
        self.assertAlmostEqual(stats.max_realized_drawdown, 1.63)
        stats, _ = run_config(SimConfig(), [market(result="no", quotes=((5, .29, .30),)), market(2)])
        self.assertAlmostEqual(stats.net_pnl, .68)
        self.assertAlmostEqual(stats.max_realized_drawdown, 1.63)

    def test_pending_settlement_blocks_new_entries_until_known_availability(self):
        first = market(settled=1350, quotes=((5, .29, .30),))
        second = market(2, quotes=((5, .29, .30), (8, .29, .30)))
        _, trades = run_config(SimConfig(sell_target_cents=None), [first, second], details=True)
        self.assertEqual(trades[1]["entry_minute"], 8)

    def test_optional_stop_executes_at_observed_bid_with_costs(self):
        stats, trades = run_config(SimConfig(stop_loss_cents=10),
            [market(quotes=((5, .29, .30), (6, .20, .21)))], details=True)
        self.assertEqual(trades[0]["exit_reason"], "stop")
        self.assertEqual(trades[0]["exit_price"], .19)
        self.assertLess(stats.net_pnl, 0)

    def test_training_selects_even_when_a_different_config_wins_holdout(self):
        configs = [SimConfig(buy_max_cents=75, sell_target_cents=None, side=side,
                             contracts=1, slippage_cents=0) for side in ("up", "down")]
        markets = [market(i, result="yes" if i <= 2 else "no", quotes=((5, .39, .40),)) for i in range(1, 5)]
        rows, selected, _, _ = compare(configs, markets, train_fraction=.5, min_train_trades=1)
        self.assertEqual(selected["config"]["side"], "up")
        self.assertLess(selected["test"]["net_pnl"], 0)
        self.assertGreater(next(row for row in rows if row["config"]["side"] == "down")["test"]["net_pnl"], 0)

    def test_zero_trade_config_is_not_selected_over_losing_active_config(self):
        active = SimConfig(sell_target_cents=None)
        inactive = replace(active, buy_max_cents=20)
        markets = [market(i, result="no", quotes=((5, .29, .30),)) for i in range(1, 5)]
        _, selected, _, _ = compare([active, inactive], markets, train_fraction=.5, min_train_trades=1)
        self.assertEqual(selected["config_id"], active.config_id)
        self.assertLess(selected["train"]["net_pnl"], 0)

    def test_settlement_boundary_purge_prevents_training_labels_from_crossing_split(self):
        markets = [market(1, settled=901), market(2), market(3)]
        train, test, purged = chronological_split(markets, .34)
        self.assertEqual([m.ticker for m in train], [markets[0].ticker])
        self.assertEqual([m.ticker for m in purged], [markets[1].ticker])
        self.assertEqual([m.ticker for m in test], [markets[2].ticker])

    def test_invalid_configurations_and_nonfinite_money_are_rejected(self):
        for changes in ({"buy_min_cents": True}, {"buy_max_cents": float("nan")},
                        {"start_minute": 5.5}, {"end_minute": 5}, {"side": "winner"},
                        {"contracts": 0}, {"sell_target_cents": 20}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                SimConfig(**changes)
        with self.assertRaises(ValueError):
            run_config(SimConfig(), [market()], initial_cash=float("inf"))
