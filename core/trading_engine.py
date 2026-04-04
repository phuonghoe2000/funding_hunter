import asyncio
import logging
import threading
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone, timedelta

from core.multi_exchange import MultiExchangeManager
from core.opportunity import (
    DEFAULT_SLIPPAGE_PCT,
    DEFAULT_TARGET_HOURS,
    build_best_opportunity,
    build_directional_opportunity,
)
from core.session_store import SessionStore
from core.trade_advisor import (
    build_liquidation_context,
    build_liquidation_reduce_policy,
    build_monitor_advice,
    build_trade_plan,
)
from config.constants import Exchange, get_exchange_symbol, Side
from config.settings import Settings
from exchanges.okx_client import OKXClient
from exchanges.binance_client import BinanceClient
from exchanges.bingx_client import BingXClient
from exchanges.gate_client import GateClient
from exchanges.aster_client import AsterClient
from exchanges.bybit_client import BybitClient
from exchanges.base import FundingRate

logger = logging.getLogger(__name__)

RISK_REDUCE_RATIO = 0.5
RISK_REDUCE_SPLITS = 10
RISK_REDUCE_INTERVAL_SECONDS = 2.0
RISK_REDUCE_COOLDOWN_SECONDS = 60.0


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
        self.session_store = SessionStore()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    async def initialize(self) -> bool:
        """Connects the enabled exchanges (only those with API keys configured)."""
        tasks = []
        if self.settings.okx and self.settings.okx.api_key:
            tasks.append(self.exchange_manager.connect_exchange(Exchange.OKX, OKXClient(self.settings.okx)))
        if self.settings.binance and self.settings.binance.api_key:
            tasks.append(self.exchange_manager.connect_exchange(Exchange.BINANCE, BinanceClient(self.settings.binance)))
        if self.settings.bingx and self.settings.bingx.api_key:
            tasks.append(self.exchange_manager.connect_exchange(Exchange.BINGX, BingXClient(self.settings.bingx)))
        if self.settings.gate and self.settings.gate.api_key:
            tasks.append(self.exchange_manager.connect_exchange(Exchange.GATE, GateClient(self.settings.gate)))
        aster_cfg = getattr(self.settings, 'asterdex', None) or getattr(self.settings, 'aster', None)
        if aster_cfg and aster_cfg.api_key:
            ex_enum = Exchange.ASTERDEX if hasattr(Exchange, 'ASTERDEX') else getattr(Exchange, 'ASTER')
            tasks.append(self.exchange_manager.connect_exchange(ex_enum, AsterClient(aster_cfg)))
        if self.settings.bybit and self.settings.bybit.api_key:
            tasks.append(self.exchange_manager.connect_exchange(Exchange.BYBIT, BybitClient(self.settings.bybit)))
        if not tasks:
            logger.warning("No exchanges enabled in settings.")
            return False
        results = await asyncio.gather(*tasks)
        return any(results)

    async def shutdown(self):
        await self.exchange_manager.disconnect_all()

    def get_active_session(self) -> Optional[Dict[str, Any]]:
        return self.session_store.load_active_session()

    def get_recent_journal(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.session_store.read_recent_journal(limit)

    def _session_matches(
        self,
        session: Optional[Dict[str, Any]],
        pair: str,
        long_ex: Exchange,
        short_ex: Exchange,
    ) -> bool:
        if not session:
            return False
        return (
            session.get("pair") == pair
            and session.get("long_exchange") in (long_ex, long_ex.value)
            and session.get("short_exchange") in (short_ex, short_ex.value)
        )

    @staticmethod
    def _parse_session_datetime(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None
        return None

    @staticmethod
    def _remaining_risk_reduce_cooldown(session: Optional[Dict[str, Any]]) -> float:
        if not session:
            return 0.0
        last_reduce_at = TradingEngine._parse_session_datetime(session.get("last_risk_reduce_at"))
        if not last_reduce_at:
            return 0.0
        elapsed = (datetime.now(timezone.utc) - last_reduce_at).total_seconds()
        return max(0.0, RISK_REDUCE_COOLDOWN_SECONDS - elapsed)

    @staticmethod
    def _extract_position_sizes(long_pos: Any, short_pos: Any) -> Dict[str, float]:
        long_size = float(abs(getattr(long_pos, "size", 0.0) or 0.0))
        short_size = float(abs(getattr(short_pos, "size", 0.0) or 0.0))
        active_candidates = [value for value in (long_size, short_size) if value > 0]
        remaining_size = min(active_candidates) if len(active_candidates) == 2 else 0.0
        return {
            "long_size": long_size,
            "short_size": short_size,
            "remaining_size": remaining_size,
        }

    async def _fetch_exchange_market_context(self, pair: str, exchange: Exchange) -> Dict[str, Any]:
        client = self.exchange_manager.clients.get(exchange)
        if not client:
            raise RuntimeError(f"{exchange.value} not connected")

        symbol = get_exchange_symbol(pair, exchange)
        context: Dict[str, Any] = {
            "exchange": exchange,
            "symbol": symbol,
            "client": client,
        }

        try:
            context["order_book"] = await client.get_order_book(symbol, limit=5)
        except Exception as e:
            context["order_book_error"] = str(e)

        try:
            context["funding"] = await client.get_funding_rate(symbol)
        except Exception as e:
            context["funding_error"] = str(e)

        try:
            context["mark_price"] = await client.get_mark_price(symbol)
        except Exception as e:
            context["mark_price_error"] = str(e)

        try:
            context["ticker"] = await client.get_ticker(symbol)
        except Exception as e:
            context["ticker_error"] = str(e)

        try:
            context["balance"] = await client.get_balance()
        except Exception as e:
            context["balance_error"] = str(e)

        return context

    # ------------------------------------------------------------------
    # 1. Scan funding rates
    # ------------------------------------------------------------------
    async def scan_opportunities(
        self,
        min_spread: float = 0.0,
        top_n: int = 50,
        slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
        profitable_only: bool = False,
    ) -> List[Dict]:
        logger.info("Scanning for funding rate opportunities...")
        rates = await self.exchange_manager.get_all_funding_rates(top_n=top_n)
        if not rates:
            return []
        opportunities = []
        for symbol, ex_rates in rates.items():
            metrics = build_best_opportunity(
                pair=symbol,
                exchange_rates=ex_rates,
                target_hours=DEFAULT_TARGET_HOURS,
                slippage_pct=slippage_pct,
            )
            if not metrics:
                continue
            spread = metrics.gross_spread_pct / 100
            if spread < min_spread:
                continue
            if profitable_only and not metrics.profitable_after_costs:
                continue

            opportunities.append({
                "symbol": symbol,
                "short_exchange": metrics.short_exchange.value,
                "short_rate": metrics.short_rate,
                "short_interval": metrics.short_interval_hours,
                "short_norm": metrics.short_norm_rate,
                "long_exchange": metrics.long_exchange.value,
                "long_rate": metrics.long_rate,
                "long_interval": metrics.long_interval_hours,
                "long_norm": metrics.long_norm_rate,
                "spread": spread,
                "gross_spread_pct": metrics.gross_spread_pct,
                "round_trip_cost_pct": metrics.round_trip_cost_pct,
                "net_edge_pct": metrics.net_edge_pct,
                "hours_to_break_even": metrics.hours_to_break_even,
                "profitable_after_costs": metrics.profitable_after_costs,
                "next_funding_time_long": metrics.next_funding_time_long,
                "next_funding_time_short": metrics.next_funding_time_short,
            })
        opportunities.sort(key=lambda x: (x["net_edge_pct"], x["spread"]), reverse=True)
        return opportunities

    # ------------------------------------------------------------------
    # 2. Status (balances + positions)
    # ------------------------------------------------------------------
    async def get_status(self) -> Dict[str, dict]:
        status = {}
        total_balance = 0.0
        total_available = 0.0
        total_active_positions = 0
        for ex_enum, client in self.exchange_manager.clients.items():
            name = ex_enum.value
            try:
                balance = await client.get_balance()
                positions = await client.get_all_positions()
                active = [p for p in positions if float(p.size) != 0]
                total_balance += getattr(balance, "total", 0.0) or 0.0
                total_available += getattr(balance, "available", 0.0) or 0.0
                total_active_positions += len(active)
                status[name] = {"connected": True, "balance": balance,
                                "active_positions": len(active), "positions": active}
            except Exception as e:
                logger.error(f"Error getting status for {name}: {e}")
                status[name] = {"connected": False, "error": str(e)}
        status["summary"] = {
            "connected_exchanges": len(self.exchange_manager.clients),
            "total_balance": total_balance,
            "total_available": total_available,
            "total_active_positions": total_active_positions,
        }
        active_session = self.get_active_session()
        if active_session:
            status["active_session"] = active_session
        return status

    # ------------------------------------------------------------------
    # 3. Open hedged position (analyze + split entry)
    # ------------------------------------------------------------------
    async def open_position(self, pair: str, long_ex_name: str, short_ex_name: str,
                            size: float, leverage: int = 3, splits: int = 1,
                            price_spread_min: Optional[float] = None,
                            skip_leverage: bool = False,
                            analyze_duration: float = 120.0,
                            skip_spread_check: bool = False) -> Dict:
        long_ex = _resolve_exchange(long_ex_name)
        short_ex = _resolve_exchange(short_ex_name)
        pair_info = {}
        economics = None
        trade_plan = None
        session_id = self.session_store.new_session_id()

        self.session_store.append_journal_event(
            "open_requested_cli",
            {
                "session_id": session_id,
                "pair": pair,
                "long_exchange": long_ex,
                "short_exchange": short_ex,
                "size": size,
                "leverage": leverage,
                "splits": splits,
                "skip_spread_check": skip_spread_check,
            },
        )

        try:
            pair_info = await self.get_pair_info(
                pair,
                long_ex_name,
                short_ex_name,
                requested_size_tokens=size,
                leverage=leverage,
            )
            selected_trade = pair_info.get("selected_trade")
            trade_plan = pair_info.get("trade_plan")
            if selected_trade:
                economics = selected_trade
                logger.info(
                    f"Funding edge ({DEFAULT_TARGET_HOURS:.0f}H): gross {selected_trade['gross_spread_pct']:.4f}% | "
                    f"cost {selected_trade['round_trip_cost_pct']:.4f}% | net {selected_trade['net_edge_pct']:.4f}%"
                )
            if trade_plan:
                logger.info(
                    f"Execution plan: {trade_plan['recommendation']} | "
                    f"score {trade_plan['quality_score']:.1f}/100 | "
                    f"suggested size {trade_plan['recommended_size_tokens']:.6f}"
                )
                for blocker in trade_plan.get("blockers", []):
                    logger.warning(f"Blocker: {blocker}")
                for warning in trade_plan.get("warnings", [])[:3]:
                    logger.warning(f"Warning: {warning}")
                if trade_plan.get("recommendation") == "AVOID":
                    logger.warning("Pre-trade plan says AVOID. Continuing because CLI command was explicit.")
        except Exception as e:
            logger.warning(f"Could not evaluate funding economics before opening: {e}")

        # Step 1: Analyze spread to determine threshold (unless skipping check)
        if price_spread_min is None and not skip_spread_check:
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
        elif price_spread_min is None and skip_spread_check:
            price_spread_min = -100.0  # safe default since we skip checking anyway

        # Step 2: Open via MultiExchangeManager with splits
        logger.info(f"🚀 Opening {pair}: LONG {long_ex_name} / SHORT {short_ex_name} | "
                     f"Size: ${size} | Leverage: {leverage}x | Splits: {splits}")

        open_result = await self.exchange_manager.open_hedged_position_split(
            pair=pair, long_exchange=long_ex, short_exchange=short_ex,
            total_size=size, leverage=leverage,
            split_count=splits,
            delay_between_splits=2.0,
            price_spread_min=price_spread_min,
            spread_check_interval=2.0,
            max_wait_per_split=3600.0,
            log_callback=lambda m: logger.info(m),
            skip_leverage_set=skip_leverage,
            skip_spread_check=skip_spread_check
        )

        if economics:
            open_result["economics"] = economics
            if economics["net_edge_pct"] <= 0:
                open_result.setdefault("warnings", []).append(
                    "Funding edge is below estimated round-trip cost."
                )
        if trade_plan:
            open_result["trade_plan"] = trade_plan

        if open_result.get("success") and open_result.get("splits_completed", 0) > 0:
            initial_long_rate = pair_info.get("long", {}).get("funding_rate") or 0.0
            initial_short_rate = pair_info.get("short", {}).get("funding_rate") or 0.0
            opened_size = open_result.get("total_long_size") or open_result.get("total_short_size") or size
            session_payload = {
                "session_id": session_id,
                "pair": pair,
                "long_exchange": long_ex,
                "short_exchange": short_ex,
                "size": opened_size,
                "long_size": open_result.get("total_long_size", opened_size),
                "short_size": open_result.get("total_short_size", opened_size),
                "leverage": leverage,
                "open_time": datetime.now(timezone.utc),
                "initial_long_rate": initial_long_rate,
                "initial_short_rate": initial_short_rate,
                "initial_net_funding": initial_short_rate - initial_long_rate,
                "pretrade_assessment": trade_plan,
                "initial_net_edge_pct": (trade_plan or {}).get("net_edge_pct", 0.0),
                "initial_quality_score": (trade_plan or {}).get("quality_score", 0.0),
                "entry_result": open_result,
            }
            self.session_store.save_active_session(session_payload)
            self.session_store.append_journal_event("position_opened_cli", session_payload)
            open_result["session"] = session_payload
        elif open_result.get("error"):
            self.session_store.append_journal_event(
                "open_failed_cli",
                {
                    "session_id": session_id,
                    "pair": pair,
                    "long_exchange": long_ex,
                    "short_exchange": short_ex,
                    "result": open_result,
                },
            )
        return open_result

    # ------------------------------------------------------------------
    # 4. Close hedged position (analyze + split exit)
    # ------------------------------------------------------------------
    async def close_position(self, pair: str, long_ex_name: str, short_ex_name: str,
                             splits: int = 1, price_spread_min: Optional[float] = None,
                             analyze_duration: float = 120.0,
                             skip_spread_check: bool = False) -> Dict:
        long_ex = _resolve_exchange(long_ex_name)
        short_ex = _resolve_exchange(short_ex_name)
        active_session = self.get_active_session()

        self.session_store.append_journal_event(
            "close_requested_cli",
            {
                "session_id": active_session.get("session_id") if self._session_matches(active_session, pair, long_ex, short_ex) else None,
                "pair": pair,
                "long_exchange": long_ex,
                "short_exchange": short_ex,
                "splits": splits,
                "skip_spread_check": skip_spread_check,
            },
        )

        # Step 1: Analyze close spread (unless skipping check)
        if price_spread_min is None and not skip_spread_check:
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
        elif price_spread_min is None and skip_spread_check:
            price_spread_min = -100.0

        # Step 2: Close with splits
        logger.info(f"🛑 Closing {pair} in {splits} split(s)...")
        close_result = await self.exchange_manager.close_hedged_position_split(
            pair=pair, long_exchange=long_ex, short_exchange=short_ex,
            splits=splits, interval_seconds=2.0,
            price_spread_min=price_spread_min, spread_check_interval=2.0,
            progress_callback=lambda s, t, m: logger.info(f"   {m}"),
            skip_spread_check=skip_spread_check
        )
        if close_result.get("success"):
            if self._session_matches(active_session, pair, long_ex, short_ex):
                self.session_store.append_journal_event(
                    "position_closed_cli",
                    {
                        "session_id": active_session.get("session_id"),
                        "pair": pair,
                        "result": close_result,
                    },
                )
                self.session_store.clear_active_session()
        else:
            self.session_store.append_journal_event(
                "close_failed_cli",
                {
                    "session_id": active_session.get("session_id") if self._session_matches(active_session, pair, long_ex, short_ex) else None,
                    "pair": pair,
                    "result": close_result,
                },
            )
        return close_result

    async def reduce_position_on_risk(
        self,
        pair: str,
        long_ex_name: str,
        short_ex_name: str,
        *,
        risk_percent: Optional[float] = None,
        threshold_percent: Optional[float] = None,
        reason: str = "risk_threshold",
        reduce_ratio: float = RISK_REDUCE_RATIO,
        splits: int = RISK_REDUCE_SPLITS,
        interval_seconds: float = RISK_REDUCE_INTERVAL_SECONDS,
        policy_mode: str = "risk_reduce",
        action_label: str = "REDUCE_50",
    ) -> Dict[str, Any]:
        long_ex = _resolve_exchange(long_ex_name)
        short_ex = _resolve_exchange(short_ex_name)
        active_session = self.get_active_session()
        cooldown_remaining = self._remaining_risk_reduce_cooldown(active_session)

        if cooldown_remaining > 0 and self._session_matches(active_session, pair, long_ex, short_ex):
            return {
                "success": False,
                "error": f"Risk reduce cooldown active for {cooldown_remaining:.1f}s",
                "cooldown_remaining": cooldown_remaining,
            }

        check_result = await self.exchange_manager.check_positions(pair, long_ex, short_ex)
        long_pos = check_result.get("long")
        short_pos = check_result.get("short")
        if not long_pos or not short_pos:
            result = {
                "success": False,
                "error": "Both legs must still be open for fast risk reduce",
                "check_result": check_result,
            }
            self.session_store.append_journal_event(
                "risk_reduce_failed_cli",
                {
                    "session_id": active_session.get("session_id") if self._session_matches(active_session, pair, long_ex, short_ex) else None,
                    "pair": pair,
                    "reason": reason,
                    "risk_percent": risk_percent,
                    "threshold_percent": threshold_percent,
                    "result": result,
                },
            )
            return result

        sizes = self._extract_position_sizes(long_pos, short_pos)
        current_size = sizes["remaining_size"]
        if current_size <= 0:
            return {
                "success": False,
                "error": "No balanced size available to reduce",
                "check_result": check_result,
            }

        close_size = current_size * reduce_ratio
        close_size_param: Optional[float] = close_size
        if reduce_ratio >= 0.999:
            close_size_param = None
        if close_size <= 0:
            return {
                "success": False,
                "error": f"Computed reduce size is invalid: {close_size}",
            }

        logger.warning(
            f"[monitor] {action_label} triggered on {pair}: close {reduce_ratio*100:.0f}% "
            f"({close_size:.6f} tokens) in {splits} split(s), every {interval_seconds:.1f}s"
        )
        self.session_store.append_journal_event(
            "risk_reduce_requested_cli",
            {
                "session_id": active_session.get("session_id") if self._session_matches(active_session, pair, long_ex, short_ex) else None,
                "pair": pair,
                "long_exchange": long_ex,
                "short_exchange": short_ex,
                "reason": reason,
                "risk_percent": risk_percent,
                "threshold_percent": threshold_percent,
                "reduce_ratio": reduce_ratio,
                "close_size": close_size,
                "splits": splits,
                "interval_seconds": interval_seconds,
                "policy_mode": policy_mode,
                "action_label": action_label,
            },
        )

        result = await self.exchange_manager.close_hedged_position_split(
            pair=pair,
            long_exchange=long_ex,
            short_exchange=short_ex,
            splits=splits,
            interval_seconds=interval_seconds,
            price_spread_min=-100.0,
            spread_check_interval=2.0,
            progress_callback=lambda _s, _t, message: logger.info(f"   {message}"),
            skip_spread_check=True,
            close_size=close_size_param,
        )
        result.update(
            {
                "mode": "risk_reduce",
                "policy_mode": policy_mode,
                "action_label": action_label,
                "reduce_ratio": reduce_ratio,
                "requested_close_size": close_size,
                "risk_percent": risk_percent,
                "threshold_percent": threshold_percent,
                "splits_used": splits,
                "interval_seconds_used": interval_seconds,
            }
        )

        post_check = await self.exchange_manager.check_positions(pair, long_ex, short_ex)
        post_long = post_check.get("long")
        post_short = post_check.get("short")
        remaining_sizes = self._extract_position_sizes(post_long, post_short)
        result["post_check"] = post_check
        result["remaining_long_size"] = remaining_sizes["long_size"]
        result["remaining_short_size"] = remaining_sizes["short_size"]
        result["remaining_size_tokens"] = remaining_sizes["remaining_size"]

        if self._session_matches(active_session, pair, long_ex, short_ex):
            updated_session = dict(active_session or {})
            if post_long and post_short:
                updated_session["long_size"] = remaining_sizes["long_size"]
                updated_session["short_size"] = remaining_sizes["short_size"]
                updated_session["size"] = remaining_sizes["remaining_size"]
                updated_session["last_risk_reduce_at"] = datetime.now(timezone.utc)
                updated_session["last_risk_reduce_reason"] = reason
                updated_session["risk_reduce_count"] = int(updated_session.get("risk_reduce_count", 0) or 0) + 1
                updated_session["latest_risk_reduce"] = {
                    "risk_percent": risk_percent,
                    "threshold_percent": threshold_percent,
                    "reduce_ratio": reduce_ratio,
                    "requested_close_size": close_size,
                    "remaining_size_tokens": remaining_sizes["remaining_size"],
                    "result_success": result.get("success", False),
                    "policy_mode": policy_mode,
                    "action_label": action_label,
                    "splits_used": splits,
                    "interval_seconds_used": interval_seconds,
                }
                self.session_store.save_active_session(updated_session)
                result["session"] = updated_session
            elif result.get("success"):
                self.session_store.clear_active_session()
                result["session_cleared"] = True

        event_type = "risk_reduce_completed_cli" if result.get("success") else "risk_reduce_failed_cli"
        self.session_store.append_journal_event(
            event_type,
            {
                "session_id": active_session.get("session_id") if self._session_matches(active_session, pair, long_ex, short_ex) else None,
                "pair": pair,
                "reason": reason,
                "risk_percent": risk_percent,
                "threshold_percent": threshold_percent,
                "reduce_ratio": reduce_ratio,
                "policy_mode": policy_mode,
                "action_label": action_label,
                "result": result,
            },
        )
        return result

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
                               auto_reduce_liq_distance: Optional[float] = None,
                               auto_close_reversal: bool = False,
                               funding_spread_min: float = 0.0001,
                               interval: float = 5.0,
                               size: Optional[float] = None,
                               leverage: Optional[int] = None,
                               auto_close_on_advice: bool = False) -> None:
        long_ex = _resolve_exchange(long_ex_name)
        short_ex = _resolve_exchange(short_ex_name)
        active_session = self.get_active_session()
        session_matches = self._session_matches(active_session, pair, long_ex, short_ex)
        tracked_position: Dict[str, Any] = {
            "pair": pair,
            "long_exchange": long_ex,
            "short_exchange": short_ex,
        }

        if session_matches and active_session:
            tracked_position.update(active_session)
        if leverage is not None:
            tracked_position["leverage"] = leverage
        leverage = int(tracked_position.get("leverage", leverage or 1) or 1)
        if size is not None:
            tracked_position["size"] = size
        tracked_size = float(tracked_position.get("size", size or 0.0) or 0.0)
        last_advice_signature = None

        total_balance = 0.0
        for _, client in self.exchange_manager.clients.items():
            try:
                balance = await client.get_balance()
                if balance:
                    total_balance += balance.available
            except Exception:
                pass
        if total_balance <= 0:
            total_balance = 1000.0
        logger.info(f"[monitor] Total balance for risk calc: ${total_balance:.2f}")
        if session_matches and active_session:
            logger.info(
                f"[monitor] Recovered session {active_session.get('session_id')} | "
                f"initial edge {active_session.get('initial_net_edge_pct', 0.0):.4f}% | "
                f"quality {active_session.get('initial_quality_score', 0.0):.1f}/100"
            )

        initial_net_funding = tracked_position.get("initial_net_funding", 0.0) or 0.0
        initial_rates = {}
        initial_rate_objects: Dict[Exchange, FundingRate] = {}
        if auto_close_reversal:
            if tracked_position.get("initial_long_rate") is not None:
                initial_rates[long_ex] = tracked_position.get("initial_long_rate", 0.0)
            if tracked_position.get("initial_short_rate") is not None:
                initial_rates[short_ex] = tracked_position.get("initial_short_rate", 0.0)
            for ex in [long_ex, short_ex]:
                client = self.exchange_manager.clients.get(ex)
                if client:
                    try:
                        sym = get_exchange_symbol(pair, ex)
                        fr = await client.get_funding_rate(sym)
                        initial_rate_objects[ex] = fr
                        initial_rates[ex] = fr.funding_rate
                    except Exception:
                        pass
            if long_ex in initial_rates and short_ex in initial_rates:
                initial_net_funding = initial_rates[short_ex] - initial_rates[long_ex]
                tracked_position["initial_long_rate"] = initial_rates[long_ex]
                tracked_position["initial_short_rate"] = initial_rates[short_ex]
                tracked_position["initial_net_funding"] = initial_net_funding
                logger.info(f"[monitor] Initial net funding: {initial_net_funding*100:.6f}%")
                if long_ex in initial_rate_objects and short_ex in initial_rate_objects:
                    economics = build_directional_opportunity(
                        pair=pair,
                        long_exchange=long_ex,
                        short_exchange=short_ex,
                        long_rate_obj=initial_rate_objects[long_ex],
                        short_rate_obj=initial_rate_objects[short_ex],
                    )
                    logger.info(
                        f"   Gross edge ({DEFAULT_TARGET_HOURS:.0f}H): {economics.gross_spread_pct:.4f}% | "
                        f"Cost: {economics.round_trip_cost_pct:.4f}% | Net: {economics.net_edge_pct:.4f}%"
                    )

        logger.info(f"[monitor] Monitoring {pair} | LONG: {long_ex_name} | SHORT: {short_ex_name}")
        logger.info(
            f"   Auto-reduce risk: {auto_close_risk}% | "
            f"Auto-reduce liq distance: {auto_reduce_liq_distance}% | "
            f"Reversal: {auto_close_reversal}"
        )
        logger.info(f"   Auto-close on monitor advice: {auto_close_on_advice}")
        logger.info("   Press Ctrl+C to stop.")

        try:
            while True:
                result = await self.exchange_manager.check_positions(pair, long_ex, short_ex)
                long_pos = result.get("long")
                short_pos = result.get("short")

                long_pnl = long_pos.unrealized_pnl if long_pos else 0
                short_pnl = short_pos.unrealized_pnl if short_pos else 0
                net_pnl = long_pnl + short_pnl

                losing = 0.0
                if long_pnl < 0:
                    losing += abs(long_pnl)
                if short_pnl < 0:
                    losing += abs(short_pnl)
                risk_pct = (losing / total_balance) * 100
                liquidation_context = build_liquidation_context(position=tracked_position, check_result=result)
                min_liq_distance_pct = liquidation_context.get("min_liquidation_distance_pct")
                nearest_liq_exchange = liquidation_context.get("nearest_liquidation_exchange")

                color_tag = "GREEN" if risk_pct < 2 else ("YELLOW" if risk_pct < 5 else "RED")
                status_line = (
                    f"{color_tag} Risk: {risk_pct:.2f}% | "
                    f"Long({long_ex_name}): ${long_pnl:+.2f} | "
                    f"Short({short_ex_name}): ${short_pnl:+.2f} | "
                    f"Net: ${net_pnl:+.2f}"
                )
                if min_liq_distance_pct is not None and nearest_liq_exchange:
                    status_line += f" | LiqDist({nearest_liq_exchange}): {min_liq_distance_pct:.2f}%"
                logger.info(status_line)

                if tracked_size <= 0 and long_pos and short_pos:
                    tracked_size = min(abs(long_pos.size), abs(short_pos.size))
                    tracked_position["size"] = tracked_size

                trade_plan = None
                if tracked_size > 0:
                    try:
                        pair_info = await self.get_pair_info(
                            pair,
                            long_ex_name,
                            short_ex_name,
                            requested_size_tokens=tracked_size,
                            leverage=leverage,
                        )
                        trade_plan = pair_info.get("trade_plan")
                        if trade_plan and not tracked_position.get("initial_net_edge_pct"):
                            tracked_position["initial_net_edge_pct"] = trade_plan.get("net_edge_pct", 0.0)
                            tracked_position["initial_quality_score"] = trade_plan.get("quality_score", 0.0)
                    except Exception as e:
                        logger.warning(f"Could not refresh trade plan while monitoring: {e}")

                monitor_advice = build_monitor_advice(
                    position=tracked_position,
                    trade_plan=trade_plan,
                    risk_percent=risk_pct,
                    check_result=result,
                ).to_dict()
                advice_signature = f"{monitor_advice['action']}:{monitor_advice['reason']}"
                if advice_signature != last_advice_signature:
                    last_advice_signature = advice_signature
                    logger.info(
                        f"[monitor] Advice: {monitor_advice['action']} | {monitor_advice['reason']} | "
                        f"net edge {monitor_advice['current_net_edge_pct']:.4f}% | "
                        f"next cycle ${monitor_advice['expected_next_cycle_pnl_usd']:+.2f}"
                    )
                    if session_matches and active_session:
                        self.session_store.append_journal_event(
                            "monitor_advice_cli",
                            {
                                "session_id": active_session.get("session_id"),
                                "pair": pair,
                                "advice": monitor_advice,
                                "risk_percent": risk_pct,
                                "min_liquidation_distance_pct": min_liq_distance_pct,
                            },
                        )

                if session_matches and active_session:
                    active_session["size"] = tracked_size or active_session.get("size")
                    active_session["latest_trade_plan"] = trade_plan
                    active_session["latest_monitor_advice"] = monitor_advice
                    active_session["long_pnl"] = long_pnl
                    active_session["short_pnl"] = short_pnl
                    active_session["latest_liquidation"] = liquidation_context
                    self.session_store.save_active_session(active_session)

                if result.get("one_side_missing"):
                    missing_ex = result["one_side_missing"]
                    logger.warning(f"[monitor] ONE SIDE MISSING on {missing_ex.value}! Possible liquidation!")

                if auto_close_on_advice and monitor_advice["action"] in {"EMERGENCY_CLOSE", "CLOSE_NOW"}:
                    logger.warning(f"[monitor] Auto-closing due to monitor advice: {monitor_advice['reason']}")
                    close_result = await self.close_position(
                        pair,
                        long_ex_name,
                        short_ex_name,
                        splits=1,
                        skip_spread_check=True,
                    )
                    logger.info(f"Close result: {close_result}")
                    break

                liq_policy = build_liquidation_reduce_policy(
                    distance_pct=min_liq_distance_pct,
                    activation_threshold_pct=auto_reduce_liq_distance,
                )
                if liq_policy:
                    cooldown_remaining = self._remaining_risk_reduce_cooldown(active_session or tracked_position)
                    if cooldown_remaining > 0:
                        logger.warning(
                            f"[monitor] Liq-distance reduce cooldown active for {cooldown_remaining:.1f}s "
                            f"(distance {min_liq_distance_pct:.2f}% <= threshold {auto_reduce_liq_distance}%)"
                        )
                    else:
                        logger.warning(
                            f"[monitor] Liq distance {min_liq_distance_pct:.2f}% on {nearest_liq_exchange} "
                            f"<= threshold {auto_reduce_liq_distance}%! "
                            f"Policy {liq_policy.action_label}: {liq_policy.description}"
                        )
                        reduce_result = await self.reduce_position_on_risk(
                            pair,
                            long_ex_name,
                            short_ex_name,
                            risk_percent=risk_pct,
                            threshold_percent=auto_reduce_liq_distance,
                            reason="monitor_liq_distance_threshold",
                            reduce_ratio=liq_policy.reduce_ratio,
                            splits=liq_policy.splits,
                            interval_seconds=liq_policy.interval_seconds,
                            policy_mode=liq_policy.mode,
                            action_label=liq_policy.action_label,
                        )
                        logger.info(f"Risk reduce result: {reduce_result}")
                        if reduce_result.get("success"):
                            tracked_position["size"] = reduce_result.get("remaining_size_tokens", tracked_position.get("size", 0.0))
                            tracked_position["long_size"] = reduce_result.get("remaining_long_size", tracked_position.get("long_size", 0.0))
                            tracked_position["short_size"] = reduce_result.get("remaining_short_size", tracked_position.get("short_size", 0.0))
                            tracked_position["last_risk_reduce_at"] = datetime.now(timezone.utc)
                            tracked_position["risk_reduce_count"] = int(tracked_position.get("risk_reduce_count", 0) or 0) + 1
                            active_session = self.get_active_session()
                            if active_session and self._session_matches(active_session, pair, long_ex, short_ex):
                                tracked_position.update(active_session)
                            continue
                        logger.warning(f"[monitor] Liq-distance reduce failed: {reduce_result.get('error')}")

                if auto_close_risk is not None and risk_pct >= auto_close_risk:
                    cooldown_remaining = self._remaining_risk_reduce_cooldown(active_session or tracked_position)
                    if cooldown_remaining > 0:
                        logger.warning(
                            f"[monitor] Risk reduce cooldown active for {cooldown_remaining:.1f}s "
                            f"(risk {risk_pct:.2f}% >= threshold {auto_close_risk}%)"
                        )
                    else:
                        logger.warning(
                            f"[monitor] Risk {risk_pct:.2f}% >= threshold {auto_close_risk}%! "
                            "Triggering fast both-legs reduce..."
                        )
                        reduce_result = await self.reduce_position_on_risk(
                            pair,
                            long_ex_name,
                            short_ex_name,
                            risk_percent=risk_pct,
                            threshold_percent=auto_close_risk,
                            reason="monitor_risk_threshold",
                        )
                        logger.info(f"Risk reduce result: {reduce_result}")
                        if reduce_result.get("success"):
                            tracked_position["size"] = reduce_result.get("remaining_size_tokens", tracked_position.get("size", 0.0))
                            tracked_position["long_size"] = reduce_result.get("remaining_long_size", tracked_position.get("long_size", 0.0))
                            tracked_position["short_size"] = reduce_result.get("remaining_short_size", tracked_position.get("short_size", 0.0))
                            tracked_position["last_risk_reduce_at"] = datetime.now(timezone.utc)
                            tracked_position["risk_reduce_count"] = int(tracked_position.get("risk_reduce_count", 0) or 0) + 1
                            active_session = self.get_active_session()
                            if active_session and self._session_matches(active_session, pair, long_ex, short_ex):
                                tracked_position.update(active_session)
                            continue
                        else:
                            logger.warning(f"[monitor] Risk reduce failed: {reduce_result.get('error')}")

                if auto_close_reversal and initial_net_funding != 0:
                    should_close, reason = await self._check_funding_reversal(
                        pair, long_ex, short_ex, initial_net_funding, initial_rates, funding_spread_min
                    )
                    if should_close:
                        logger.warning(f"[monitor] Funding reversal: {reason}")
                        logger.info("Auto-closing due to funding reversal...")
                        close_result = await self.close_position(
                            pair,
                            long_ex_name,
                            short_ex_name,
                            splits=1,
                            skip_spread_check=True,
                        )
                        logger.info(f"Close result: {close_result}")
                        break

                await asyncio.sleep(interval)
        except KeyboardInterrupt:
            logger.info("[monitor] Monitoring stopped by user.")

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
                    "net_pnl": result.get(
                        "net_pnl",
                        result.get("realized_pnl", 0) - result.get("commission", 0) + result.get("funding_fee", 0),
                    ),
                }
            except Exception as e:
                pnl_data[label] = {"exchange": ex.value, "error": str(e)}

        # Totals
        total_rpnl = sum(d.get("realized_pnl", 0) for d in pnl_data.values() if "error" not in d)
        total_comm = sum(d.get("commission", 0) for d in pnl_data.values() if "error" not in d)
        total_fund = sum(d.get("funding_fee", 0) for d in pnl_data.values() if "error" not in d)
        total_net = sum(d.get("net_pnl", 0) for d in pnl_data.values() if "error" not in d)
        pnl_data["total"] = {
            "realized_pnl": total_rpnl, "commission": total_comm,
            "funding_fee": total_fund, "net_pnl": total_net,
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
    async def get_pair_info(
        self,
        pair: str,
        long_ex_name: str,
        short_ex_name: str,
        requested_size_tokens: float = 0.0,
        leverage: int = 1,
    ) -> Dict:
        long_ex = _resolve_exchange(long_ex_name)
        short_ex = _resolve_exchange(short_ex_name)
        info = {}
        funding_objs: Dict[Exchange, FundingRate] = {}
        balances: Dict[Exchange, Any] = {}
        market_contexts: Dict[Exchange, Dict[str, Any]] = {}

        results = await asyncio.gather(
            self._fetch_exchange_market_context(pair, long_ex),
            self._fetch_exchange_market_context(pair, short_ex),
            return_exceptions=True,
        )

        for (label, ex), result in zip([("long", long_ex), ("short", short_ex)], results):
            if isinstance(result, Exception):
                info[label] = {"exchange": ex.value, "error": str(result)}
                continue

            market_contexts[ex] = result
            funding = result.get("funding")
            balance = result.get("balance")
            order_book = result.get("order_book") or {}
            ticker = result.get("ticker") or {}
            if funding:
                funding_objs[ex] = funding
            if balance:
                balances[ex] = balance

            info[label] = {
                "exchange": ex.value,
                "symbol": result["symbol"],
                "mark_price": result.get("mark_price"),
                "best_bid": order_book["bids"][0][0] if order_book.get("bids") else None,
                "best_ask": order_book["asks"][0][0] if order_book.get("asks") else None,
                "funding_rate": funding.funding_rate if funding else None,
                "funding_interval_hours": funding.funding_interval_hours if funding else None,
                "next_funding_time": funding.next_funding_time if funding else None,
                "ticker_volume": ticker.get("volume"),
                "available_balance": getattr(balance, "available", None) if balance else None,
            }

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

        if long_ex in funding_objs and short_ex in funding_objs:
            selected_trade = build_directional_opportunity(
                pair=pair,
                long_exchange=long_ex,
                short_exchange=short_ex,
                long_rate_obj=funding_objs[long_ex],
                short_rate_obj=funding_objs[short_ex],
            )
            recommended_trade = build_best_opportunity(
                pair=pair,
                exchange_rates=funding_objs,
            )
            info["selected_trade"] = selected_trade.to_dict()
            if recommended_trade:
                info["recommended_trade"] = recommended_trade.to_dict()

            if requested_size_tokens > 0:
                long_context = market_contexts.get(long_ex, {})
                short_context = market_contexts.get(short_ex, {})
                trade_plan = build_trade_plan(
                    pair=pair,
                    long_exchange=long_ex,
                    short_exchange=short_ex,
                    long_rate_obj=funding_objs[long_ex],
                    short_rate_obj=funding_objs[short_ex],
                    long_order_book=long_context.get("order_book"),
                    short_order_book=short_context.get("order_book"),
                    long_ticker=long_context.get("ticker"),
                    short_ticker=short_context.get("ticker"),
                    long_balance=balances.get(long_ex),
                    short_balance=balances.get(short_ex),
                    requested_size_tokens=requested_size_tokens,
                    leverage=leverage,
                    long_price=info["long"].get("best_ask") or info["long"].get("mark_price"),
                    short_price=info["short"].get("best_bid") or info["short"].get("mark_price"),
                )
                info["trade_plan"] = trade_plan.to_dict()
        return info
