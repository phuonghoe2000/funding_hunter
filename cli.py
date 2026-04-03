#!/usr/bin/env python3
"""
Funding Hunter CLI - Full Feature Parity with GUI
Commands: scan, status, positions, info, open, close, monitor, pnl, analyze, session, journal
"""
import asyncio
import argparse
import logging
import json
import os
import sys
from datetime import datetime, timezone
from dataclasses import is_dataclass, asdict
from enum import Enum
from tabulate import tabulate

from core.config_manager import ConfigManager
from core.trading_engine import TradingEngine

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


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
logging.basicConfig(level=logging.WARNING, format='%(message)s')
# Only show INFO from CLI and trading engine, suppress exchange/WS noise
logging.getLogger("__main__").setLevel(logging.INFO)
logging.getLogger("core.trading_engine").setLevel(logging.INFO)
logging.getLogger("core.multi_exchange").setLevel(logging.INFO)
# Silence all exchange client and websocket logs
for _mod in ["exchanges", "exchanges.binance_client", "exchanges.bingx_client",
             "exchanges.okx_client", "exchanges.gate_client", "exchanges.aster_client",
             "exchanges.websocket_manager"]:
    logging.getLogger(_mod).setLevel(logging.CRITICAL)
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
  python cli.py monitor --pair BTC/USDT --long gate --short binance --auto-reduce-risk 10 --auto-reduce-liq-distance 3
  python cli.py pnl --pair BTC/USDT --long gate --short binance
  python cli.py analyze --pair BTC/USDT --long gate --short binance --duration 120
  python cli.py session
  python cli.py journal --limit 20
        """
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # ── scan ──
    p = sub.add_parser("scan", help="Scan funding rate opportunities across all exchanges")
    p.add_argument("--min-spread", type=float, default=0.0, help="Min normalized 4H spread %% (default: 0)")
    p.add_argument("--top", type=int, default=30, help="Show top N results (default: 30)")
    p.add_argument("--slippage-pct", type=float, default=0.02, help="Estimated slippage per trade side in %% (default: 0.02)")
    p.add_argument("--profitable-only", action="store_true", help="Only show opportunities that stay positive after estimated costs")

    # ── status ──
    sub.add_parser("status", help="Show balances and active positions on all exchanges")

    # ── positions ──
    sub.add_parser("positions", help="Load and detect hedged positions across exchanges")

    # ── info ──
    p = sub.add_parser("info", help="Show detailed pair info: prices, funding, order book")
    p.add_argument("--pair", required=True, help="Trading pair (e.g. BTC/USDT)")
    p.add_argument("--long", required=True, help="Long exchange name")
    p.add_argument("--short", required=True, help="Short exchange name")
    p.add_argument("--size", type=float, default=0.0, help="Requested token size to evaluate trade plan")
    p.add_argument("--leverage", type=int, default=1, help="Leverage for trade plan sizing")

    # ── open ──
    p = sub.add_parser("open", help="Open hedged position with spread analysis & DCA splits")
    p.add_argument("--pair", required=True, help="Trading pair (e.g. BTC/USDT)")
    p.add_argument("--long", required=True, help="Long exchange")
    p.add_argument("--short", required=True, help="Short exchange")
    p.add_argument("--size", type=float, required=True, help="Position size in base asset (token quantity, e.g. 500 for 500 KITE)")
    p.add_argument("--leverage", type=int, default=3, help="Leverage (default: 3)")
    p.add_argument("--splits", type=int, default=1, help="Number of DCA splits (default: 1)")
    p.add_argument("--price-spread-min", type=float, default=None, help="Skip analyze; use this spread threshold")
    p.add_argument("--skip-leverage", action="store_true", help="Skip leverage setup (faster entry)")
    p.add_argument("--analyze-duration", type=float, default=120.0, help="Spread analysis duration in seconds")
    p.add_argument("--skip-spread-check", action="store_true", help="Skip spread analysis and execute splits immediately")

    # ── close ──
    p = sub.add_parser("close", help="Close hedged position with spread analysis & splits")
    p.add_argument("--pair", required=True, help="Trading pair")
    p.add_argument("--long", required=True, help="Long exchange")
    p.add_argument("--short", required=True, help="Short exchange")
    p.add_argument("--splits", type=int, default=1, help="Number of close splits (default: 1)")
    p.add_argument("--price-spread-min", type=float, default=None, help="Skip analyze; use this spread threshold")
    p.add_argument("--analyze-duration", type=float, default=120.0, help="Spread analysis duration")
    p.add_argument("--skip-spread-check", action="store_true", help="Skip spread analysis and execute splits immediately")

    # ── monitor ──
    p = sub.add_parser("monitor", help="Live monitor position PnL, risk %%, with optional fast risk-reduce")
    p.add_argument("--pair", required=True, help="Trading pair")
    p.add_argument("--long", required=True, help="Long exchange")
    p.add_argument("--short", required=True, help="Short exchange")
    p.add_argument(
        "--auto-reduce-risk",
        "--auto-close-risk",
        dest="auto_close_risk",
        type=float,
        default=None,
        help="Reduce both legs by 50%% when risk >= X%%",
    )
    p.add_argument(
        "--auto-reduce-liq-distance",
        type=float,
        default=None,
        help="Reduce both legs by 50%% when nearest liquidation distance <= X%%",
    )
    p.add_argument("--auto-close-reversal", action="store_true", help="Auto-close on funding reversal")
    p.add_argument("--auto-close-on-advice", action="store_true", help="Auto-close on CLOSE_NOW or EMERGENCY_CLOSE advice")
    p.add_argument("--funding-spread-min", type=float, default=0.01, help="Min funding spread %% for reversal check")
    p.add_argument("--interval", type=float, default=5.0, help="Check interval in seconds (default: 5)")
    p.add_argument("--size", type=float, default=None, help="Override monitored token size")
    p.add_argument("--leverage", type=int, default=None, help="Override leverage used in monitor trade-plan evaluation")

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

    sub.add_parser("session", help="Show the currently persisted active session")

    p = sub.add_parser("journal", help="Show recent trade journal events")
    p.add_argument("--limit", type=int, default=20, help="Number of recent events to show")

    return parser


# ── Command Handlers ───────────────────────────────────────────────
async def cmd_scan(engine, args):
    opps = await engine.scan_opportunities(
        min_spread=args.min_spread,
        top_n=args.top,
        slippage_pct=args.slippage_pct,
        profitable_only=args.profitable_only,
    )
    if not opps:
        logger.info("No opportunities found.")
        return
    table = []
    for opp in opps[:args.top]:
        break_even = opp["hours_to_break_even"]
        break_even_text = f"{break_even:.1f}h" if break_even != float("inf") else "inf"
        table.append([
            opp['symbol'],
            f"{opp['gross_spread_pct']:.4f}%",
            f"{opp['round_trip_cost_pct']:.4f}%",
            f"{opp['net_edge_pct']:.4f}%",
            f"{opp['short_exchange'].upper()} (-)",
            f"{opp['short_rate']*100:.4f}% ({opp['short_interval']}h)",
            f"{opp['long_exchange'].upper()} (+)",
            f"{opp['long_rate']*100:.4f}% ({opp['long_interval']}h)",
            break_even_text,
            "YES" if opp["profitable_after_costs"] else "NO",
        ])
    print(tabulate(table,
                   headers=[
                       "Symbol", "Gross 4H", "Cost", "Net Edge", "Short EX",
                       "Short Rate (interval)", "Long EX", "Long Rate (interval)",
                       "Break-even", "Profitable",
                   ],
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
    info = await engine.get_pair_info(
        args.pair,
        args.long,
        args.short,
        requested_size_tokens=args.size,
        leverage=args.leverage,
    )
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
    selected_trade = info.get("selected_trade")
    if selected_trade:
        logger.info("\n  Selected Direction Economics:")
        logger.info(f"    Gross Funding Edge (4H): {selected_trade['gross_spread_pct']:.4f}%")
        logger.info(f"    Round-trip Cost:         {selected_trade['round_trip_cost_pct']:.4f}%")
        logger.info(f"    Net Edge:                {selected_trade['net_edge_pct']:.4f}%")
        logger.info(f"    Break-even Time:         {selected_trade['hours_to_break_even']:.2f}h")
        logger.info(f"    Profitable After Cost:   {'YES' if selected_trade['profitable_after_costs'] else 'NO'}")
    recommended_trade = info.get("recommended_trade")
    if recommended_trade:
        logger.info(
            f"\n  Recommended Direction: LONG {recommended_trade['long_exchange']} / "
            f"SHORT {recommended_trade['short_exchange']}"
        )
    trade_plan = info.get("trade_plan")
    if trade_plan:
        logger.info("\n  Trade Plan:")
        logger.info(f"    Recommendation:         {trade_plan['recommendation']}")
        logger.info(f"    Quality Score:          {trade_plan['quality_score']:.1f}/100")
        logger.info(f"    Requested Size:         {trade_plan['requested_size_tokens']:.6f}")
        logger.info(f"    Suggested Size:         {trade_plan['recommended_size_tokens']:.6f}")
        logger.info(f"    Expected Next-Cycle:    ${trade_plan['expected_net_pnl_next_cycle_usd']:+.2f}")
        logger.info(f"    Net Edge (4H):          {trade_plan['net_edge_pct']:.4f}%")
        logger.info(f"    Depth Ratio:            {trade_plan['depth_ratio']:.2f}x")
        logger.info(f"    Divergence:             {trade_plan['price_divergence_pct']:.4f}%")
        for blocker in trade_plan.get("blockers", []):
            logger.info(f"    Blocker:                {blocker}")
        for warning in trade_plan.get("warnings", [])[:3]:
            logger.info(f"    Warning:                {warning}")


async def cmd_open(engine, args):
    result = await engine.open_position(
        pair=args.pair, long_ex_name=args.long, short_ex_name=args.short,
        size=args.size, leverage=args.leverage, splits=args.splits,
        price_spread_min=args.price_spread_min, skip_leverage=args.skip_leverage,
        analyze_duration=args.analyze_duration,
        skip_spread_check=args.skip_spread_check
    )
    _json(result)


async def cmd_close(engine, args):
    result = await engine.close_position(
        pair=args.pair, long_ex_name=args.long, short_ex_name=args.short,
        splits=args.splits, price_spread_min=args.price_spread_min,
        analyze_duration=args.analyze_duration,
        skip_spread_check=args.skip_spread_check
    )
    _json(result)


async def cmd_monitor(engine, args):
    await engine.monitor_position(
        pair=args.pair, long_ex_name=args.long, short_ex_name=args.short,
        auto_close_risk=args.auto_close_risk,
        auto_reduce_liq_distance=args.auto_reduce_liq_distance,
        auto_close_reversal=args.auto_close_reversal,
        funding_spread_min=args.funding_spread_min / 100,  # Convert % to decimal
        interval=args.interval,
        size=args.size,
        leverage=args.leverage,
        auto_close_on_advice=args.auto_close_on_advice,
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


async def cmd_session(engine, args):
    session = engine.get_active_session()
    if not session:
        logger.info("No active session persisted.")
        return
    _json(session)


async def cmd_journal(engine, args):
    events = engine.get_recent_journal(limit=args.limit)
    if not events:
        logger.info("Trade journal is empty.")
        return

    table = []
    for event in events:
        payload = event.get("payload", {})
        pair = payload.get("pair", "-")
        action = payload.get("advice", {}).get("action") or payload.get("result", {}).get("success")
        table.append([
            event.get("timestamp", "-"),
            event.get("event_type", "-"),
            pair,
            action if action is not None else "-",
        ])
    print(tabulate(table, headers=["Timestamp", "Event", "Pair", "Detail"], tablefmt="grid"))


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
    "session": cmd_session,
    "journal": cmd_journal,
}


async def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    config = ConfigManager("user_config.json").get_settings()
    engine = TradingEngine(config)
    if args.command in {"session", "journal"}:
        handler = COMMAND_MAP.get(args.command)
        if handler:
            await handler(engine, args)
        return

    # Initialize (suppress noisy print() from exchange clients during connect)
    _real_stdout = sys.stdout
    sys.stdout = open(os.devnull, 'w')
    try:
        connected = await engine.initialize()
    finally:
        sys.stdout.close()
        sys.stdout = _real_stdout
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
