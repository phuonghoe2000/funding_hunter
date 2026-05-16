import unittest

from core.basis_checker_service import (
    BasisMarketData,
    build_funding_aligned_candidates,
    compute_executable_basis,
    format_basis_signal_message,
    load_ignored_symbols,
)


class BasisCheckerServiceTests(unittest.TestCase):
    def test_positive_mark_basis_recommends_long_binance_short_aster(self):
        bn_book = {"asks": [(100.0, 1.0)], "bids": [(99.9, 1.0)]}
        as_book = {"asks": [(101.2, 1.0)], "bids": [(101.0, 1.0)]}

        result = compute_executable_basis(1.0, bn_book, as_book)

        self.assertEqual(result["rec_long"], "binance")
        self.assertEqual(result["rec_short"], "aster")
        self.assertAlmostEqual(result["exec_basis"], 1.0)
        self.assertGreater(result["directional_exec_basis"], 0)

    def test_negative_mark_basis_recommends_long_aster_short_binance(self):
        bn_book = {"asks": [(101.2, 1.0)], "bids": [(101.0, 1.0)]}
        as_book = {"asks": [(100.0, 1.0)], "bids": [(99.9, 1.0)]}

        result = compute_executable_basis(-1.0, bn_book, as_book)

        self.assertEqual(result["rec_long"], "aster")
        self.assertEqual(result["rec_short"], "binance")
        self.assertAlmostEqual(result["exec_basis"], 1.0)
        self.assertLess(result["directional_exec_basis"], 0)

    def test_candidates_require_funding_alignment(self):
        market_data = BasisMarketData(
            binance_data={
                "AAAUSDT": {"mark_price": 100.0, "index_price": 100.0, "last_funding": 0.0001},
                "BBBUSDT": {"mark_price": 100.0, "index_price": 100.0, "last_funding": 0.0005},
            },
            aster_data={
                "AAAUSDT": {"mark_price": 101.0, "index_price": 101.0, "last_funding": 0.0005},
                "BBBUSDT": {"mark_price": 101.0, "index_price": 101.0, "last_funding": 0.0001},
            },
            bn_intervals={"AAAUSDT": 8, "BBBUSDT": 8},
            as_intervals={"AAAUSDT": 8, "BBBUSDT": 8},
            common_symbols=["AAAUSDT", "BBBUSDT"],
        )

        rows = build_funding_aligned_candidates(market_data)

        self.assertEqual([row["symbol"] for row in rows], ["AAAUSDT"])

    def test_signal_message_contains_hedge_guidance(self):
        message = format_basis_signal_message(
            symbol="BTCUSDT",
            rec_long="binance",
            rec_short="aster",
            mark_basis=0.75,
            exec_basis=0.62,
            fr_diff=0.21,
            reference_price=100000.0,
            min_basis=0.5,
            min_funding_diff=0.2,
        )

        self.assertIn("BTCUSDT", message)
        self.assertIn("Long: <b>BINANCE</b>", message)
        self.assertIn("Short: <b>ASTER</b>", message)
        self.assertIn("Executable basis: +0.6200%", message)

    def test_load_ignored_symbols_drops_expired_entries(self):
        cfg = {"telegram_ignored_symbols": {"123": {"AAAUSDT": 200.0, "BBBUSDT": 50.0}}}

        ignored = load_ignored_symbols(cfg, now=100.0)

        self.assertEqual(ignored, {"123": {"AAAUSDT": 200.0}})


if __name__ == "__main__":
    unittest.main()
