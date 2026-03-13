import asyncio
import logging
import threading
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone, timedelta

from core.multi_exchange import MultiExchangeManager
from config.constants import Exchange, get_exchange_symbol, Side
from config.settings import Settings
from exchanges.okx_client import OKXClient
from exchanges.binance_client import BinanceClient
from exchanges.bingx_client import BingXClient
from exchanges.gate_client import GateClient
from exchanges.aster_client import AsterClient
from exchanges.base import FundingRate

logger = logging.getLogger(__name__)


def _resolve_exchange(name: str) -> Exchange:
    """Convert a string exchange name to an Exchange enum."""
    for ex in Exchange:
        if ex.value.lower() == name.lower():
            return ex
    raise ValueError(f"Unknown exchange: {name}")


class TradingEngine:
    """Core trading engine decoupled from UI for CLI use."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.exchange_manager = MultiExchangeManager()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    async def initialize(self) -> bool:
        """Connects the enabled exchanges."""
        tasks = []
        if self.settings.okx:
            tasks.append(self.exchange_manager.connect_exchange(Exchange.OKX, OKXClient(self.settings.okx)))
        if self.settings.binance:
            tasks.append(self.exchange_manager.connect_exchange(Exchange.BINANCE, BinanceClient(self.settings.binance)))
        if self.settings.bingx:
            tasks.append(self.exchange_manager.connect_exchange(Exchange.BINGX, BingXClient(self.settings.bingx)))
        if self.settings.gate:
            tasks.append(self.exchange_manager.connect_exchange(Exchange.GATE, GateClient(self.settings.gate)))
        if getattr(self.settings, 'asterdex', None) or getattr(self.settings, 'aster', None):
            cfg = getattr(self.settings, 'asterdex', getattr(self.settings, 'aster', None))
            ex_enum = Exchange.ASTERDEX if hasattr(Exchange, 'ASTERDEX') else getattr(Exchange, 'ASTER')
            tasks.append(self.exchange_manager.connect_exchange(ex_enum, AsterClient(cfg)))
        if not tasks:
            logger.warning("No exchanges enabled in settings.")
            return False
        results = await asyncio.gather(*tasks)
        return any(results)

    async def shutdown(self):
        await self.exchange_manager.disconnect_all()

    # ------------------------------------------------------------------
    # 1. Scan funding rates
    # ------------------------------------------------------------------
    async def scan_opportunities(self, min_spread: float = 0.0) -> List[Dict]:
        logger.info("Scanning for funding rate opportunities...")
        rates = await self.exchange_manager.get_all_funding_rates()
        if not rates:
            return []
        opportunities = []
        for symbol, ex_rates in rates.items():
            if len(ex_rates) < 2:
                continue
            normalized = []
            for ex_enum, rate in ex_rates.items():
                norm = rate.funding_rate * (4.0 / max(1, rate.funding_interval_hours))
                normalized.append({
                    "exchange": ex_enum.value, "rate": rate.funding_rate,
                    "interval": rate.funding_interval_hours, "normalized_rate": norm,
                    "next_funding_time": rate.next_funding_time,
                })
            normalized.sort(key=lambda x: x["normalized_rate"], reverse=True)
            spread = normalized[0]["normalized_rate"] - normalized[-1]["normalized_rate"]
            if spread >= min_spread:
                opportunities.append({
                    "symbol": symbol,
                    "short_exchange": normalized[0]["exchange"],
                    "short_rate": normalized[0]["rate"],
                    "short_interval": normalized[0]["interval"],
                    "short_norm": normalized[0]["normalized_rate"],
                    "long_exchange": normalized[-1]["exchange"],
                    "long_rate": normalized[-1]["rate"],
                    "long_interval": normalized[-1]["interval"],
                    "long_norm": normalized[-1]["normalized_rate"],
                    "spread": spread,
                })
        opportunities.sort(key=lambda x: x["spread"], reverse=True)
        return opportunities

    # ------------------------------------------------------------------
    # 2. Status (balances + positions)
    # ------------------------------------------------------------------
    async def get_status(self) -> Dict[str, dict]:
        status = {}
        for ex_enum, client in self.exchange_manager.clients.items():
            name = ex_enum.value
            try:
                balance = await client.get_balance()
                positions = await client.get_all_positions()
                active = [p for p in positions if float(p.size) != 0]
                status[name] = {"connected": True, "balance": balance,
                                "active_positions": len(active), "positions": active}
            except Exception as e:
                logger.error(f"Error getting status for {name}: {e}")
                status[name] = {"connected": False, "error": str(e)}
        return status

    # ------------------------------------------------------------------
    # 3. Open hedged position (analyze + split entry)
    # ------------------------------------------------------------------
    async def open_position(self, pair: str, long_ex_name: str, short_ex_name: str,
                            size: float, leverage: int = 3, splits: int = 1,
                            price_spread_min: Optional[float] = None,
                            skip_leverage: bool = False,
                            analyze_duration: float = 120.0) -> Dict:
        long_ex = _resolve_exchange(long_ex_name)
        short_ex = _resolve_exchange(short_ex_name)

        # Step 1: Analyze spread to determine threshold
        if price_spread_min is None:
            logger.info(f"📊 Analyzing open spread for {analyze_duration:.0f}s...")
            result = await self.exchange_manager.analyze_spread(
                pair, long_ex, short_ex,
                duration_seconds=analyze_duration, check_interval=2.0,
                log_callback=lambda m: logger.info(m), mode="open"
            )
            if result.get("success"):
                price_spread_min = result["second_best_spread"]
                logger.info(f"✅ Analyze done. Threshold = {price_spread_min:.4f}%")
            else:
                price_spread_min = -100.0
                logger.warning(f"Analyze failed: {result.get('error')}. Using no threshold.")

        # Step 2: Open via MultiExchangeManager with splits
        logger.info(f"🚀 Opening {pair}: LONG {long_ex_name} / SHORT {short_ex_name} | "
                     f"Size: ${size} | Leverage: {leverage}x | Splits: {splits}")

        open_result = await self.exchange_manager.open_hedged_position(
            pair=pair, long_exchange=long_ex, short_exchange=short_ex,
            size=size, leverage=leverage
        )
        return open_result

    # ------------------------------------------------------------------
    # 4. Close hedged position (analyze + split exit)
    # ------------------------------------------------------------------
    async def close_position(self, pair: str, long_ex_name: str, short_ex_name: str,
                             splits: int = 1, price_spread_min: Optional[float] = None,
                             analyze_duration: float = 120.0) -> Dict:
        long_ex = _resolve_exchange(long_ex_name)
        short_ex = _resolve_exchange(short_ex_name)

        # Step 1: Analyze close spread
        if price_spread_min is None:
            logger.info(f"📊 Analyzing close spread for {analyze_duration:.0f}s...")
            result = await self.exchange_manager.analyze_spread(
                pair, long_ex, short_ex,
                duration_seconds=analyze_duration, check_interval=2.0,
                log_callback=lambda m: logger.info(m), mode="close"
            )
            if result.get("success"):
                price_spread_min = result["second_best_spread"]
                logger.info(f"✅ Analyze done. Threshold = {price_spread_min:.4f}%")
            else:
                price_spread_min = -100.0
                logger.warning(f"Analyze failed. Using no threshold.")

        # Step 2: Close with splits
        logger.info(f"🛑 Closing {pair} in {splits} split(s)...")
        close_result = await self.exchange_manager.close_hedged_position_split(
            pair=pair, long_exchange=long_ex, short_exchange=short_ex,
            splits=splits, interval_seconds=2.0,
            price_spread_min=price_spread_min, spread_check_interval=2.0,
            progress_callback=lambda s, t, m: logger.info(f"   {m}"),
        )
        return close_result

    # ------------------------------------------------------------------
    # 5. Load existing positions (find hedged pairs)
    # ------------------------------------------------------------------
    async def load_existing_positions(self) -> Dict:
        all_positions = await self.exchange_manager.scan_all_positions()
        # Build a map: base_symbol -> list of (exchange, position)
        symbol_map: Dict[str, List] = {}
        for ex_enum, positions in all_positions.items():
            for pos in positions:
                if pos.size == 0:
                    continue
                # Normalize symbol to BASE/QUOTE
                raw = pos.symbol
                # Try to extract base pair name
                base = raw.replace("-USDT", "/USDT").replace("_USDT", "/USDT")
                if "/" not in base:
                    # e.g. BTCUSDT -> BTC/USDT
                    base = base.replace("USDT", "/USDT")
                base = base.replace("-SWAP", "")
                if base not in symbol_map:
                    symbol_map[base] = []
                symbol_map[base].append({
                    "exchange": ex_enum.value,
                    "side": pos.side.value if hasattr(pos.side, 'value') else str(pos.side),
                    "size": pos.size,
                    "entry_price": pos.entry_price,
                    "unrealized_pnl": pos.unrealized_pnl,
                    "leverage": pos.leverage,
                })

        # Find hedged pairs (positions on 2+ exchanges with opposite sides)
        hedged = {}
        for pair, pos_list in symbol_map.items():
            longs = [p for p in pos_list if p["side"].lower() in ("long", "buy")]
            shorts = [p for p in pos_list if p["side"].lower() in ("short", "sell")]
            if longs and shorts:
                hedged[pair] = {"long": longs, "short": shorts}

        return {"all_positions": symbol_map, "hedged_pairs": hedged}

    # ------------------------------------------------------------------
    # 6. Monitor position (blocking loop with PnL + risk + auto-close)
    # ------------------------------------------------------------------
    async def monitor_position(self, pair: str, long_ex_name: str, short_ex_name: str,
                               auto_close_risk: Optional[float] = None,
                               auto_close_reversal: bool = False,
                               funding_spread_min: float = 0.0001,
                               interval: float = 5.0) -> None:
        long_ex = _resolve_exchange(long_ex_name)
        short_ex = _resolve_exchange(short_ex_name)

        # Get total balance for risk calculation
        total_balance = 0.0
        for _, client in self.exchange_manager.clients.items():
            try:
                b = await client.get_balance()
                if b:
                    total_balance += b.available
            except:
                pass
        if total_balance <= 0:
            total_balance = 1000.0
        logger.info(f"📊 Total balance for risk calc: ${total_balance:.2f}")

        # Get initial funding rates for reversal detection
        initial_net_funding = 0.0
        initial_rates = {}
        if auto_close_reversal:
            for ex in [long_ex, short_ex]:
                client = self.exchange_manager.clients.get(ex)
                if client:
                    try:
                        sym = get_exchange_symbol(pair, ex)
                        fr = await client.get_funding_rate(sym)
                        initial_rates[ex] = fr.funding_rate
                    except:
                        pass
            if long_ex in initial_rates and short_ex in initial_rates:
                initial_net_funding = initial_rates[short_ex] - initial_rates[long_ex]
                logger.info(f"📊 Initial net funding: {initial_net_funding*100:.6f}%")

        logger.info(f"👁 Monitoring {pair} | LONG: {long_ex_name} | SHORT: {short_ex_name}")
        logger.info(f"   Auto-close risk: {auto_close_risk}% | Reversal: {auto_close_reversal}")
        logger.info(f"   Press Ctrl+C to stop.")

        try:
            while True:
                result = await self.exchange_manager.check_positions(pair, long_ex, short_ex)
                long_pos = result.get("long")
                short_pos = result.get("short")

                long_pnl = long_pos.unrealized_pnl if long_pos else 0
                short_pnl = short_pos.unrealized_pnl if short_pos else 0
                net_pnl = long_pnl + short_pnl

                # Risk = |losing side| / balance * 100
                losing = 0.0
                if long_pnl < 0: losing += abs(long_pnl)
                if short_pnl < 0: losing += abs(short_pnl)
                risk_pct = (losing / total_balance) * 100

                color_tag = "🟢" if risk_pct < 2 else ("🟡" if risk_pct < 5 else "🔴")
                logger.info(
                    f"{color_tag} Risk: {risk_pct:.2f}% | "
                    f"Long({long_ex_name}): ${long_pnl:+.2f} | "
                    f"Short({short_ex_name}): ${short_pnl:+.2f} | "
                    f"Net: ${net_pnl:+.2f}"
                )

                # One side missing = potential liquidation
                if result.get("one_side_missing"):
                    missing_ex = result["one_side_missing"]
                    logger.warning(f"⚠️ ONE SIDE MISSING on {missing_ex.value}! Possible liquidation!")

                # Auto-close on risk
                if auto_close_risk is not None and risk_pct >= auto_close_risk:
                    logger.warning(f"🚨 Risk {risk_pct:.2f}% >= threshold {auto_close_risk}%! Auto-closing...")
                    close_result = await self.close_position(pair, long_ex_name, short_ex_name, splits=1)
                    logger.info(f"Close result: {close_result}")
                    break

                # Auto-close on funding reversal
                if auto_close_reversal and initial_net_funding != 0:
                    should_close, reason = await self._check_funding_reversal(
                        pair, long_ex, short_ex, initial_net_funding, initial_rates, funding_spread_min
                    )
                    if should_close:
                        logger.warning(f"🔄 Funding reversal: {reason}")
                        logger.info("Auto-closing due to funding reversal...")
                        close_result = await self.close_position(pair, long_ex_name, short_ex_name, splits=1)
                        logger.info(f"Close result: {close_result}")
                        break

                await asyncio.sleep(interval)
        except KeyboardInterrupt:
            logger.info("🛑 Monitoring stopped by user.")

    async def _check_funding_reversal(self, pair, long_ex, short_ex,
                                       initial_net, initial_rates, min_threshold) -> tuple:
        try:
            current_rates = {}
            for ex in [long_ex, short_ex]:
                client = self.exchange_manager.clients.get(ex)
                if client:
                    sym = get_exchange_symbol(pair, ex)
                    fr = await client.get_funding_rate(sym)
                    current_rates[ex] = fr.funding_rate
            if len(current_rates) < 2:
                return False, ""
            current_net = current_rates[short_ex] - current_rates[long_ex]
            # Check direction reversal
            if (initial_net * current_net) < 0:
                return True, f"Direction reversed: {initial_net*100:.6f}% -> {current_net*100:.6f}%"
            # Check spread too low
            if abs(current_net) < min_threshold:
                return True, f"Spread {abs(current_net)*100:.6f}% < threshold {min_threshold*100:.6f}%"
            # Check 97% drop
            if abs(initial_net) > 0 and (1 - abs(current_net)/abs(initial_net)) > 0.97:
                return True, f"Spread dropped >97%: {abs(initial_net)*100:.6f}% -> {abs(current_net)*100:.6f}%"
            return False, ""
        except Exception as e:
            logger.error(f"Funding reversal check error: {e}")
            return False, ""

    # ------------------------------------------------------------------
    # 7. PnL calculation via Trade/Income History
    # ------------------------------------------------------------------
    async def get_pnl(self, pair: str, long_ex_name: str, short_ex_name: str,
                      since: Optional[datetime] = None) -> Dict:
        long_ex = _resolve_exchange(long_ex_name)
        short_ex = _resolve_exchange(short_ex_name)
        if since is None:
            since = datetime.now(timezone.utc) - timedelta(hours=24)
        start_time = int(since.timestamp() * 1000)

        pnl_data = {}
        for label, ex in [("long", long_ex), ("short", short_ex)]:
            client = self.exchange_manager.clients.get(ex)
            if not client:
                pnl_data[label] = {"error": f"{ex.value} not connected"}
                continue
            try:
                symbol = get_exchange_symbol(pair, ex)
                result = await client.get_closed_pnl(symbol, start_time)
                pnl_data[label] = {
                    "exchange": ex.value,
                    "realized_pnl": result.get("realized_pnl", 0),
                    "commission": result.get("commission", 0),
                    "funding_fee": result.get("funding_fee", 0),
                    "net_pnl": result.get("realized_pnl", 0) + result.get("commission", 0) + result.get("funding_fee", 0),
                }
            except Exception as e:
                pnl_data[label] = {"exchange": ex.value, "error": str(e)}

        # Totals
        total_rpnl = sum(d.get("realized_pnl", 0) for d in pnl_data.values() if "error" not in d)
        total_comm = sum(d.get("commission", 0) for d in pnl_data.values() if "error" not in d)
        total_fund = sum(d.get("funding_fee", 0) for d in pnl_data.values() if "error" not in d)
        pnl_data["total"] = {
            "realized_pnl": total_rpnl, "commission": total_comm,
            "funding_fee": total_fund, "net_pnl": total_rpnl + total_comm + total_fund,
        }
        return pnl_data

    # ------------------------------------------------------------------
    # 8. Analyze spread (open/close mode)
    # ------------------------------------------------------------------
    async def analyze_spread(self, pair: str, long_ex_name: str, short_ex_name: str,
                             duration: float = 120.0, mode: str = "open") -> Dict:
        long_ex = _resolve_exchange(long_ex_name)
        short_ex = _resolve_exchange(short_ex_name)
        return await self.exchange_manager.analyze_spread(
            pair, long_ex, short_ex,
            duration_seconds=duration, check_interval=2.0,
            log_callback=lambda m: logger.info(m), mode=mode
        )

    # ------------------------------------------------------------------
    # 9. Pair info (prices, funding, order book)
    # ------------------------------------------------------------------
    async def get_pair_info(self, pair: str, long_ex_name: str, short_ex_name: str) -> Dict:
        long_ex = _resolve_exchange(long_ex_name)
        short_ex = _resolve_exchange(short_ex_name)
        info = {}
        for label, ex in [("long", long_ex), ("short", short_ex)]:
            client = self.exchange_manager.clients.get(ex)
            if not client:
                info[label] = {"error": f"{ex.value} not connected"}
                continue
            try:
                symbol = get_exchange_symbol(pair, ex)
                book = await client.get_order_book(symbol, limit=5)
                funding = await client.get_funding_rate(symbol)
                mark = await client.get_mark_price(symbol)
                info[label] = {
                    "exchange": ex.value,
                    "symbol": symbol,
                    "mark_price": mark,
                    "best_bid": book["bids"][0][0] if book.get("bids") else None,
                    "best_ask": book["asks"][0][0] if book.get("asks") else None,
                    "funding_rate": funding.funding_rate,
                    "funding_interval_hours": funding.funding_interval_hours,
                    "next_funding_time": funding.next_funding_time,
                }
            except Exception as e:
                info[label] = {"exchange": ex.value, "error": str(e)}

        # Spread calculation
        try:
            lb = info["long"].get("best_ask", 0) or 0
            sa = info["short"].get("best_bid", 0) or 0
            if lb and sa:
                avg = (lb + sa) / 2
                info["open_spread_pct"] = ((sa - lb) / avg) * 100
            lb2 = info["long"].get("best_bid", 0) or 0
            sa2 = info["short"].get("best_ask", 0) or 0
            if lb2 and sa2:
                avg2 = (lb2 + sa2) / 2
                info["close_spread_pct"] = ((lb2 - sa2) / avg2) * 100
        except:
            pass
        return info
