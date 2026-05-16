import unittest
from unittest.mock import AsyncMock

from core.gui_service import GUIWorkflowService


class PeriodicFundingSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_periodic_summary_only_counts_new_funding_records(self):
        service = GUIWorkflowService(settings=None)
        service.get_funding_fee_history = AsyncMock(
            return_value={
                "pair": "BTC/USDT",
                "strategy_type": "futures_hedge",
                "legs": [],
                "records": [
                    {"time": 1000, "exchange": "binance", "label": "LONG", "symbol": "BTCUSDT", "income": -1.0},
                    {"time": 2000, "exchange": "asterdex", "label": "SHORT", "symbol": "BTCUSDT", "income": 2.5},
                    {"time": 3000, "exchange": "binance", "label": "LONG", "symbol": "BTCUSDT", "income": 1.25},
                ],
                "total": 2.75,
            }
        )
        service.build_monitor_snapshot = AsyncMock(return_value={"long_pnl": 10.0, "short_pnl": -3.0})

        summary = await service.build_periodic_funding_pnl_summary(
            {
                "pair": "BTC/USDT",
                "session_id": "session-1",
                "last_periodic_funding_fee_time": 2000,
                "last_periodic_funding_fee_key": "2000|asterdex|SHORT|BTCUSDT|2.5",
            }
        )

        self.assertEqual(len(summary["new_records"]), 1)
        self.assertEqual(summary["new_records"][0]["time"], 3000)
        self.assertAlmostEqual(summary["new_funding_fee"], 1.25)
        self.assertAlmostEqual(summary["total_funding_fee"], 2.75)
        self.assertAlmostEqual(summary["unrealized_pnl"], 7.0)
        self.assertAlmostEqual(summary["net_pnl_estimate"], 9.75)
        self.assertEqual(summary["newest_record_time"], 3000)


if __name__ == "__main__":
    unittest.main()
