#!/usr/bin/env python3
"""
Funding Hunter CLI - Full Feature Parity with GUI
Commands: scan, status, positions, info, open, close, monitor, pnl, analyze
"""
import asyncio
import argparse
import logging
import json
import sys
from datetime import datetime, timezone
from dataclasses import is_dataclass, asdict
from enum import Enum
from tabulate import tabulate

from core.config_manager import ConfigManager
from core.trading_engine import TradingEngine


class DataclassEncoder(json.JSONEncoder):
    def default(self, obj):
        if is_dataclass(obj):
            return asdict(obj)
        if isinstance(obj, Enum):
            return obj.value
        try:
            return super().default(obj)
        except TypeError:
            return str(obj)


def _json(data):
    print(json.dumps(data, indent=2, cls=DataclassEncoder))


# ── Logging ────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)


# ── CLI Argument Parsing ───────────────────────────────────────────
def build_parser():
    parser = argparse.ArgumentParser(
        description="🎯 Funding Hunter CLI - Multi Exchange Funding Rate Arbitrage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python cli.py scan --min-spread 0.001
  python cli.py status
  python cli.py positions
  python cli.py info --pair BTC/USDT --long binance --short bingx
  python cli.py open --pair BTC/USDT --long gate --short binance --size 100 --leverage 3
  python cli.py close --pair BTC/USDT --long gate --short binance --splits 3
  python cli.py monitor --pair BTC/USDT --long gate --short binance --auto-close-risk 10
  python cli.py pnl --pair BTC/USDT --long gate --short binance
  python cli.py analyze --pair BTC/USDT --long gate --short binance --duration 120
        """
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # ── scan ──
    p = sub.add_parser("scan", help="Scan funding rate opportunities across all exchanges")
    p.add_argument("--min-spread", type=float, default=0.0, help="Min normalized 4H spread %% (default: 0)")
    p.add_argument("--top", type=int, default=30, help="Show top N results (default: 30)")

    # ── status ──
    sub.add_parser("status", help="Show balances and active positions on all exchanges")

    # ── positions ──
    sub.add_parser("positions", help="Load and detect hedged positions across exchanges")

    # ── info ──
    p = sub.add_parser("info", help="Show detailed pair info: prices, funding, order book")
    p.add_argument("--pair", required=True, help="Trading pair (e.g. BTC/USDT)")
    p.add_argument("--long", required=True, help="Long exchange name")
    p.add_argument("--short", required=True, help="Short exchange name")

    # ── open ──
    p = sub.add_parser("open", help="Open hedged position with spread analysis & DCA splits")
    p.add_argument("--pair", required=True, help="Trading pair (e.g. BTC/USDT)")
    p.add_argument("--long", required=True, help="Long exchange")
    p.add_argument("--short", required=True, help="Short exchange")
    p.add_argument("--size", type=float, required=True, help="Position size in USDT")
    p.add_argument("--leverage", type=int, default=3, help="Leverage (default: 3)")
    p.add_argument("--splits", type=int, default=1, help="Number of DCA splits (default: 1)")
    p.add_argument("--price-spread-min", type=float, default=None, help="Skip analyze; use this spread threshold")
    p.add_argument("--skip-leverage", action="store_true", help="Skip leverage setup (faster entry)")
    p.add_argument("--analyze-duration", type=float, default=120.0, help="Spread analysis duration in seconds")

    # ── close ──
    p = sub.add_parser("close", help="Close hedged position with spread analysis & splits")
    p.add_argument("--pair", required=True, help="Trading pair")
    p.add_argument("--long", required=True, help="Long exchange")
    p.add_argument("--short", required=True, help="Short exchange")
    p.add_argument("--splits", type=int, default=1, help="Number of close splits (default: 1)")
    p.add_argument("--price-spread-min", type=float, default=None, help="Skip analyze; use this spread threshold")
    p.add_argument("--analyze-duration", type=float, default=120.0, help="Spread analysis duration")

    # ── monitor ──
    p = sub.add_parser("monitor", help="Live monitor position PnL, risk %%, with optional auto-close")
    p.add_argument("--pair", required=True, help="Trading pair")
    p.add_argument("--long", required=True, help="Long exchange")
    p.add_argument("--short", required=True, help="Short exchange")
    p.add_argument("--auto-close-risk", type=float, default=None, help="Auto-close when risk >= X%%")
    p.add_argument("--auto-close-reversal", action="store_true", help="Auto-close on funding reversal")
    p.add_argument("--funding-spread-min", type=float, default=0.01, help="Min funding spread %% for reversal check")
    p.add_argument("--interval", type=float, default=5.0, help="Check interval in seconds (default: 5)")

    # ── pnl ──
    p = sub.add_parser("pnl", help="Calculate realized PnL from trade and income history")
    p.add_argument("--pair", required=True, help="Trading pair")
    p.add_argument("--long", required=True, help="Long exchange")
    p.add_argument("--short", required=True, help="Short exchange")
    p.add_argument("--since", default=None, help="Start time (e.g. '2026-03-12 10:00:00')")

    # ── analyze ──
    p = sub.add_parser("analyze", help="Analyze price spread between two exchanges")
    p.add_argument("--pair", required=True, help="Trading pair")
    p.add_argument("--long", required=True, help="Long exchange")
    p.add_argument("--short", required=True, help="Short exchange")
    p.add_argument("--duration", type=float, default=120.0, help="Analysis duration in seconds (default: 120)")
    p.add_argument("--mode", choices=["open", "close"], default="open", help="Analysis mode")

    return parser


# ── Command Handlers ───────────────────────────────────────────────
async def cmd_scan(engine, args):
    opps = await engine.scan_opportunities(min_spread=args.min_spread)
    if not opps:
        logger.info("No opportunities found.")
        return
    table = []
    for opp in opps[:args.top]:
        table.append([
            opp['symbol'],
            f"{opp['spread']:.4f}%",
            f"{opp['short_exchange'].upper()} (-)",
            f"{opp['short_norm']:.4f}%",
            f"{opp['long_exchange'].upper()} (+)",
            f"{opp['long_norm']:.4f}%",
        ])
    print(tabulate(table,
                   headers=["Symbol", "Norm Spread (4H)", "Short EX", "Short Rate", "Long EX", "Long Rate"],
                   tablefmt="grid"))
    logger.info(f"\n✅ Found {len(opps)} opportunities (showing top {min(args.top, len(opps))})")


async def cmd_status(engine, args):
    _json(await engine.get_status())


async def cmd_positions(engine, args):
    result = await engine.load_existing_positions()
    hedged = result.get("hedged_pairs", {})
    all_pos = result.get("all_positions", {})

    if hedged:
        logger.info(f"🔗 Found {len(hedged)} hedged pair(s):\n")
        for pair, sides in hedged.items():
            logger.info(f"  📌 {pair}")
            for l in sides.get("long", []):
                logger.info(f"     LONG  {l['exchange']:>10} | Size: {l['size']} | Entry: {l['entry_price']} | uPnL: ${l['unrealized_pnl']:+.2f}")
            for s in sides.get("short", []):
                logger.info(f"     SHORT {s['exchange']:>10} | Size: {s['size']} | Entry: {s['entry_price']} | uPnL: ${s['unrealized_pnl']:+.2f}")
            logger.info("")
    else:
        logger.info("No hedged pairs detected.")

    # Also show non-hedged positions
    non_hedged = {k: v for k, v in all_pos.items() if k not in hedged}
    if non_hedged:
        logger.info(f"\n📋 Other active positions ({len(non_hedged)} pair(s)):")
        for pair, pos_list in non_hedged.items():
            for p in pos_list:
                logger.info(f"  {pair:>12} | {p['side'].upper():>5} {p['exchange']:>10} | Size: {p['size']} | uPnL: ${p['unrealized_pnl']:+.2f}")


async def cmd_info(engine, args):
    info = await engine.get_pair_info(args.pair, args.long, args.short)
    logger.info(f"\n📊 Pair Info: {args.pair}\n{'='*50}")
    for label, side in [("LONG", "long"), ("SHORT", "short")]:
        d = info.get(side, {})
        if "error" in d:
            logger.info(f"  {label} ({d.get('exchange', '?')}): ERROR - {d['error']}")
        else:
            logger.info(f"  {label} ({d['exchange']}):")
            logger.info(f"    Mark Price:   {d.get('mark_price', 'N/A')}")
            logger.info(f"    Best Bid:     {d.get('best_bid', 'N/A')}")
            logger.info(f"    Best Ask:     {d.get('best_ask', 'N/A')}")
            logger.info(f"    Funding Rate: {d.get('funding_rate', 0)*100:.6f}% ({d.get('funding_interval_hours', '?')}h)")
    if "open_spread_pct" in info:
        logger.info(f"\n  Open Spread:  {info['open_spread_pct']:.4f}%")
    if "close_spread_pct" in info:
        logger.info(f"  Close Spread: {info['close_spread_pct']:.4f}%")


async def cmd_open(engine, args):
    result = await engine.open_position(
        pair=args.pair, long_ex_name=args.long, short_ex_name=args.short,
        size=args.size, leverage=args.leverage, splits=args.splits,
        price_spread_min=args.price_spread_min, skip_leverage=args.skip_leverage,
        analyze_duration=args.analyze_duration,
    )
    _json(result)


async def cmd_close(engine, args):
    result = await engine.close_position(
        pair=args.pair, long_ex_name=args.long, short_ex_name=args.short,
        splits=args.splits, price_spread_min=args.price_spread_min,
        analyze_duration=args.analyze_duration,
    )
    _json(result)


async def cmd_monitor(engine, args):
    await engine.monitor_position(
        pair=args.pair, long_ex_name=args.long, short_ex_name=args.short,
        auto_close_risk=args.auto_close_risk,
        auto_close_reversal=args.auto_close_reversal,
        funding_spread_min=args.funding_spread_min / 100,  # Convert % to decimal
        interval=args.interval,
    )


async def cmd_pnl(engine, args):
    since = None
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    result = await engine.get_pnl(args.pair, args.long, args.short, since=since)

    logger.info(f"\n{'='*50}")
    logger.info(f"  TRADE REPORT: {args.pair}")
    logger.info(f"{'='*50}")
    for label, key in [("LONG", "long"), ("SHORT", "short")]:
        d = result.get(key, {})
        if "error" in d:
            logger.info(f"\n  {label} ({d.get('exchange', '?')}): ERROR - {d['error']}")
        else:
            logger.info(f"\n  {label} ({d['exchange']}):")
            logger.info(f"    Realized PnL:  ${d.get('realized_pnl', 0):+.2f}")
            logger.info(f"    Commission:    ${d.get('commission', 0):+.2f}")
            logger.info(f"    Funding Fees:  ${d.get('funding_fee', 0):+.2f}")
            logger.info(f"    Net PnL:       ${d.get('net_pnl', 0):+.2f}")

    t = result.get("total", {})
    logger.info(f"\n{'─'*50}")
    logger.info(f"  TOTAL Realized PnL:  ${t.get('realized_pnl', 0):+.2f}")
    logger.info(f"  TOTAL Commission:    ${t.get('commission', 0):+.2f}")
    logger.info(f"  TOTAL Funding Fees:  ${t.get('funding_fee', 0):+.2f}")
    net = t.get('net_pnl', 0)
    tag = "PROFIT ✅" if net >= 0 else "LOSS ❌"
    logger.info(f"  >>> FINAL NET PnL:   ${net:+.2f} ({tag})")
    logger.info(f"{'='*50}")


async def cmd_analyze(engine, args):
    result = await engine.analyze_spread(
        pair=args.pair, long_ex_name=args.long, short_ex_name=args.short,
        duration=args.duration, mode=args.mode,
    )
    _json(result)


# ── Main ───────────────────────────────────────────────────────────
COMMAND_MAP = {
    "scan": cmd_scan,
    "status": cmd_status,
    "positions": cmd_positions,
    "info": cmd_info,
    "open": cmd_open,
    "close": cmd_close,
    "monitor": cmd_monitor,
    "pnl": cmd_pnl,
    "analyze": cmd_analyze,
}


async def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Initialize
    config = ConfigManager("user_config.json").get_settings()
    engine = TradingEngine(config)
    connected = await engine.initialize()
    if not connected:
        logger.error("Failed to connect to any exchange. Check API keys.")
        return

    try:
        handler = COMMAND_MAP.get(args.command)
        if handler:
            await handler(engine, args)
        else:
            parser.print_help()
    finally:
        await engine.shutdown()


if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
