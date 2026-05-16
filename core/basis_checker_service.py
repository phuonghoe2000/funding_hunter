"""
Shared Binance/Asterdex basis checker data access and calculations.

This module intentionally does not import tkinter or application state so it can
be reused by the standalone basis checker and the main Funding Hunter GUI.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp


BINANCE_PREMIUM_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
ASTER_PREMIUM_URL = "https://fapi.asterdex.com/fapi/v1/premiumIndex"
BINANCE_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
BINANCE_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
ASTER_EXCHANGE_INFO_URL = "https://fapi.asterdex.com/fapi/v1/exchangeInfo"
BINANCE_DEPTH_URL = "https://fapi.binance.com/fapi/v1/depth"
ASTER_DEPTH_URL = "https://fapi.asterdex.com/fapi/v1/depth"
BINANCE_FUNDING_INFO_URL = "https://fapi.binance.com/fapi/v1/fundingInfo"
ASTER_FUNDING_INFO_URL = "https://fapi.asterdex.com/fapi/v1/fundingInfo"

DEFAULT_MIN_FUNDING_DIFF = 0.0
MIN_EXEC_BASIS_SIGNAL = 0.5
DEFAULT_ALERT_THRESHOLD = 0.3
AUTO_SIGNAL_COOLDOWN_SECONDS = 300
MAX_AUTO_SIGNALS_PER_SCAN = 3
MAX_EXECUTABLE_BASIS_CANDIDATES = 60
EXECUTABLE_BASIS_CONCURRENCY = 8
TELEGRAM_POLL_TIMEOUT_SECONDS = 20
TELEGRAM_IGNORE_DURATION_SECONDS = 3600
TELEGRAM_IGNORED_SYMBOLS_CONFIG_KEY = "telegram_ignored_symbols"

DEFAULT_CONFIG_FILE = Path(__file__).resolve().parent.parent / "basis_config.json"


@dataclass
class BasisMarketData:
    binance_data: dict[str, dict[str, float]] = field(default_factory=dict)
    aster_data: dict[str, dict[str, float]] = field(default_factory=dict)
    tickers_24h: dict[str, dict[str, float]] = field(default_factory=dict)
    bn_intervals: dict[str, int] = field(default_factory=dict)
    as_intervals: dict[str, int] = field(default_factory=dict)
    qty_precisions: dict[str, int] = field(default_factory=dict)
    common_symbols: list[str] = field(default_factory=list)


def _coerce_interval(value: Any, default: int = 8) -> int:
    try:
        interval = int(value)
    except (TypeError, ValueError):
        return default
    return interval if interval > 0 else default


async def fetch_trading_symbols(session: aiohttp.ClientSession, url: str) -> tuple[set[str], dict[str, int]]:
    """Fetch USDT futures symbols that are currently trading."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
    except Exception:
        return set(), {}

    symbols: set[str] = set()
    precisions: dict[str, int] = {}
    for item in data.get("symbols", []):
        symbol = str(item.get("symbol", ""))
        if item.get("status") == "TRADING" and symbol.endswith("USDT"):
            symbols.add(symbol)
            try:
                precisions[symbol] = int(item.get("quantityPrecision", 0))
            except (TypeError, ValueError):
                precisions[symbol] = 0
    return symbols, precisions


async def fetch_all_mark_prices(
    session: aiohttp.ClientSession,
    url: str,
    trading_only: set[str] | None = None,
) -> dict[str, dict[str, float]]:
    """Fetch mark/index/funding data for all USDT symbols from one exchange."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
    except Exception:
        return {}

    result: dict[str, dict[str, float]] = {}
    for item in data:
        symbol = str(item.get("symbol", ""))
        if not symbol.endswith("USDT"):
            continue
        if trading_only is not None and symbol not in trading_only:
            continue
        try:
            result[symbol] = {
                "mark_price": float(item.get("markPrice", 0)),
                "index_price": float(item.get("indexPrice", 0)),
                "last_funding": float(item.get("lastFundingRate", 0)),
            }
        except (TypeError, ValueError):
            continue
    return result


async def fetch_24h_tickers(session: aiohttp.ClientSession) -> dict[str, dict[str, float]]:
    """Fetch 24h Binance change and quote volume for USDT symbols."""
    try:
        async with session.get(BINANCE_TICKER_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
    except Exception:
        return {}

    result: dict[str, dict[str, float]] = {}
    for item in data:
        symbol = str(item.get("symbol", ""))
        if not symbol.endswith("USDT"):
            continue
        try:
            result[symbol] = {
                "price_change_pct": float(item.get("priceChangePercent", 0)),
                "volume": float(item.get("quoteVolume", 0)),
            }
        except (TypeError, ValueError):
            continue
    return result


async def fetch_funding_intervals(session: aiohttp.ClientSession, url: str) -> dict[str, int]:
    """Fetch funding interval hours keyed by symbol."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
    except Exception:
        return {}

    result: dict[str, int] = {}
    for item in data:
        symbol = str(item.get("symbol", ""))
        if symbol.endswith("USDT"):
            result[symbol] = _coerce_interval(item.get("fundingIntervalHours", 8))
    return result


async def fetch_basis_market_data() -> BasisMarketData:
    """Fetch Binance/Asterdex market data used by basis scans."""
    async with aiohttp.ClientSession() as session:
        (bn_trading, bn_precisions), (as_trading, as_precisions) = await asyncio.gather(
            fetch_trading_symbols(session, BINANCE_EXCHANGE_INFO_URL),
            fetch_trading_symbols(session, ASTER_EXCHANGE_INFO_URL),
        )

        binance_data, aster_data, tickers, bn_intervals, as_intervals = await asyncio.gather(
            fetch_all_mark_prices(session, BINANCE_PREMIUM_URL, bn_trading),
            fetch_all_mark_prices(session, ASTER_PREMIUM_URL, as_trading),
            fetch_24h_tickers(session),
            fetch_funding_intervals(session, BINANCE_FUNDING_INFO_URL),
            fetch_funding_intervals(session, ASTER_FUNDING_INFO_URL),
        )

    qty_precisions: dict[str, int] = {}
    for symbol in set(bn_precisions) | set(as_precisions):
        bn_precision = bn_precisions.get(symbol, 0)
        as_precision = as_precisions.get(symbol, 0)
        qty_precisions[symbol] = min(bn_precision, as_precision) if bn_precision and as_precision else bn_precision or as_precision

    return BasisMarketData(
        binance_data=binance_data,
        aster_data=aster_data,
        tickers_24h=tickers,
        bn_intervals=bn_intervals,
        as_intervals=as_intervals,
        qty_precisions=qty_precisions,
        common_symbols=sorted(set(binance_data) & set(aster_data)),
    )


async def fetch_all_data() -> tuple[
    dict[str, dict[str, float]],
    dict[str, dict[str, float]],
    dict[str, dict[str, float]],
    dict[str, int],
    dict[str, int],
    dict[str, int],
]:
    """Backward-compatible tuple return used by the standalone basis checker."""
    market_data = await fetch_basis_market_data()
    return (
        market_data.binance_data,
        market_data.aster_data,
        market_data.tickers_24h,
        market_data.bn_intervals,
        market_data.as_intervals,
        market_data.qty_precisions,
    )


def get_normalized_funding(market_data: BasisMarketData, symbol: str) -> tuple[float, float, float]:
    """Return Binance, Asterdex, and Aster-Binance funding rates normalized to 4h."""
    binance = market_data.binance_data.get(symbol)
    aster = market_data.aster_data.get(symbol)
    if not binance or not aster:
        return 0.0, 0.0, 0.0

    bn_interval = market_data.bn_intervals.get(symbol, 8) or 8
    as_interval = market_data.as_intervals.get(symbol, 8) or 8
    bn_funding = binance["last_funding"] * 100 * (4 / bn_interval)
    as_funding = aster["last_funding"] * 100 * (4 / as_interval)
    return bn_funding, as_funding, as_funding - bn_funding


def build_funding_aligned_candidates(
    market_data: BasisMarketData,
    *,
    max_candidates: int = MAX_EXECUTABLE_BASIS_CANDIDATES,
) -> list[dict[str, Any]]:
    """Build mark-basis candidates whose funding direction supports the basis."""
    rows: list[dict[str, Any]] = []
    for symbol in market_data.common_symbols:
        binance = market_data.binance_data.get(symbol)
        aster = market_data.aster_data.get(symbol)
        if not binance or not aster:
            continue

        bn_price = binance["mark_price"]
        as_price = aster["mark_price"]
        if bn_price <= 0 or as_price <= 0:
            continue

        diff = as_price - bn_price
        diff_pct = diff / bn_price * 100
        if abs(diff_pct) > 10:
            continue

        bn_funding, as_funding, funding_diff = get_normalized_funding(market_data, symbol)
        if diff_pct * funding_diff <= 0:
            continue

        ticker = market_data.tickers_24h.get(symbol)
        rows.append(
            {
                "symbol": symbol,
                "change_24h": ticker["price_change_pct"] if ticker else 0.0,
                "binance_price": bn_price,
                "aster_price": as_price,
                "diff": diff,
                "diff_pct": diff_pct,
                "bn_funding": bn_funding,
                "as_funding": as_funding,
                "funding_diff": funding_diff,
            }
        )

    rows.sort(key=lambda row: abs(row["diff_pct"]), reverse=True)
    return rows[:max_candidates]


async def fetch_order_books(symbol: str, limit: int = 10) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch Binance and Asterdex order books for one symbol."""
    async with aiohttp.ClientSession() as session:
        async def fetch_one(base_url: str) -> dict[str, Any]:
            url = f"{base_url}?symbol={symbol}&limit={limit}"
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
            except Exception:
                return {"bids": [], "asks": []}
            return {
                "bids": [(float(price), float(qty)) for price, qty in data.get("bids", [])],
                "asks": [(float(price), float(qty)) for price, qty in data.get("asks", [])],
            }

        return await asyncio.gather(
            fetch_one(BINANCE_DEPTH_URL),
            fetch_one(ASTER_DEPTH_URL),
        )


async def fetch_order_books_for_symbols(
    symbols: list[str],
    *,
    limit: int = 10,
    concurrency: int = EXECUTABLE_BASIS_CONCURRENCY,
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """Fetch Binance/Asterdex order books for multiple symbols."""
    async with aiohttp.ClientSession() as session:
        semaphore = asyncio.Semaphore(concurrency)

        async def fetch_one(symbol: str, base_url: str) -> dict[str, Any]:
            url = f"{base_url}?symbol={symbol}&limit={limit}"
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
            except Exception:
                return {"bids": [], "asks": []}
            return {
                "bids": [(float(price), float(qty)) for price, qty in data.get("bids", [])],
                "asks": [(float(price), float(qty)) for price, qty in data.get("asks", [])],
            }

        async def fetch_pair(symbol: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
            async with semaphore:
                bn_book, as_book = await asyncio.gather(
                    fetch_one(symbol, BINANCE_DEPTH_URL),
                    fetch_one(symbol, ASTER_DEPTH_URL),
                )
                return symbol, bn_book, as_book

        results = await asyncio.gather(*(fetch_pair(symbol) for symbol in symbols), return_exceptions=True)

    books: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for result in results:
        if isinstance(result, Exception):
            continue
        symbol, bn_book, as_book = result
        books[symbol] = (bn_book, as_book)
    return books


def compute_executable_basis(mark_basis: float, bn_book: dict[str, Any], as_book: dict[str, Any]) -> dict[str, Any]:
    """Return executable basis details for the direction implied by mark basis."""
    bn_ask = bn_book["asks"][0][0] if bn_book.get("asks") else 0
    as_bid = as_book["bids"][0][0] if as_book.get("bids") else 0
    bn_bid = bn_book["bids"][0][0] if bn_book.get("bids") else 0
    as_ask = as_book["asks"][0][0] if as_book.get("asks") else 0

    if mark_basis >= 0:
        exec_basis = (as_bid - bn_ask) / bn_ask * 100 if bn_ask > 0 else 0
        directional_exec_basis = exec_basis
        rec_long, rec_short = "binance", "aster"
        reference_price = (bn_ask + as_bid) / 2 if bn_ask > 0 and as_bid > 0 else 0
    else:
        exec_basis = (bn_bid - as_ask) / as_ask * 100 if as_ask > 0 else 0
        directional_exec_basis = -exec_basis
        rec_long, rec_short = "aster", "binance"
        reference_price = (as_ask + bn_bid) / 2 if as_ask > 0 and bn_bid > 0 else 0

    return {
        "exec_basis": exec_basis,
        "directional_exec_basis": directional_exec_basis,
        "rec_long": rec_long,
        "rec_short": rec_short,
        "reference_price": reference_price,
    }


async def build_executable_basis_rows(
    candidates: list[dict[str, Any]],
    *,
    order_book_limit: int = 10,
    concurrency: int = EXECUTABLE_BASIS_CONCURRENCY,
) -> list[dict[str, Any]]:
    """Enrich candidates with executable top-of-book basis."""
    books_by_symbol = await fetch_order_books_for_symbols(
        [row["symbol"] for row in candidates],
        limit=order_book_limit,
        concurrency=concurrency,
    )

    rows: list[dict[str, Any]] = []
    for row in candidates:
        books = books_by_symbol.get(row["symbol"])
        if not books:
            continue

        bn_book, as_book = books
        exec_info = compute_executable_basis(row["diff_pct"], bn_book, as_book)
        if row["diff_pct"] * exec_info["directional_exec_basis"] <= 0:
            continue

        enriched = dict(row)
        enriched.update(exec_info)
        rows.append(enriched)

    rows.sort(key=lambda row: abs(row["exec_basis"]), reverse=True)
    return rows


async def scan_executable_basis(
    *,
    max_candidates: int = MAX_EXECUTABLE_BASIS_CANDIDATES,
    order_book_limit: int = 10,
    concurrency: int = EXECUTABLE_BASIS_CONCURRENCY,
) -> tuple[BasisMarketData, list[dict[str, Any]]]:
    """Fetch market data and return executable basis rows."""
    market_data = await fetch_basis_market_data()
    candidates = build_funding_aligned_candidates(market_data, max_candidates=max_candidates)
    rows = await build_executable_basis_rows(
        candidates,
        order_book_limit=order_book_limit,
        concurrency=concurrency,
    )
    return market_data, rows


def price_format(price: float) -> str:
    """Return a format precision suited for a price magnitude."""
    if price > 1000:
        return ".2f"
    if price > 1:
        return ".4f"
    if price > 0.01:
        return ".6f"
    return ".8f"


def quantity_format(quantity: float) -> str:
    """Format a quantity for compact display."""
    if quantity >= 10000:
        return f"{quantity:,.0f}"
    if quantity >= 100:
        return f"{quantity:,.1f}"
    if quantity >= 1:
        return f"{quantity:.2f}"
    return f"{quantity:.4f}"


def load_basis_config(config_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load the basis checker config file."""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_basis_config(cfg: dict[str, Any], config_path: str | os.PathLike[str] | None = None) -> None:
    """Persist the basis checker config file."""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_FILE
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def load_ignored_symbols(cfg: dict[str, Any], *, now: float | None = None) -> dict[str, dict[str, float]]:
    """Load unexpired Telegram ignore rules from config."""
    loaded: dict[str, dict[str, float]] = {}
    raw = cfg.get(TELEGRAM_IGNORED_SYMBOLS_CONFIG_KEY, {})
    if not isinstance(raw, dict):
        return loaded

    current = now if now is not None else time.time()
    for chat_id, symbols in raw.items():
        if not isinstance(symbols, dict):
            continue
        chat_key = str(chat_id)
        for symbol, until_ts in symbols.items():
            try:
                until = float(until_ts)
            except (TypeError, ValueError):
                continue
            if until > current:
                loaded.setdefault(chat_key, {})[str(symbol).upper()] = until
    return loaded


def save_ignored_symbols(cfg: dict[str, Any], ignored_symbols_by_chat: dict[str, dict[str, float]]) -> None:
    """Persist unexpired Telegram ignore rules back into config."""
    current = time.time()
    cleaned: dict[str, dict[str, float]] = {}
    for chat_id, symbols in list(ignored_symbols_by_chat.items()):
        active = {symbol: until for symbol, until in symbols.items() if until > current}
        if active:
            cleaned[str(chat_id)] = active
    if cleaned:
        cfg[TELEGRAM_IGNORED_SYMBOLS_CONFIG_KEY] = cleaned
    else:
        cfg.pop(TELEGRAM_IGNORED_SYMBOLS_CONFIG_KEY, None)


def normalize_telegram_symbol(raw_symbol: str) -> str:
    """Normalize a symbol to the compact Telegram ignore format."""
    symbol = str(raw_symbol or "").strip().upper()
    symbol = symbol.split()[0] if symbol else ""
    for sep in ("/", "-", "_", ":"):
        symbol = symbol.replace(sep, "")
    return symbol


def format_basis_signal_message(
    *,
    symbol: str,
    rec_long: str,
    rec_short: str,
    mark_basis: float,
    exec_basis: float,
    fr_diff: float,
    reference_price: float,
    min_basis: float,
    min_funding_diff: float,
) -> str:
    """Format a Telegram signal message for a basis opportunity."""
    venue_bias = "AsterDEX richer than Binance" if mark_basis >= 0 else "Binance richer than AsterDEX"
    funding_note = "Funding supports the basis direction and passes the minimum funding diff filter."

    return (
        "<b>Basis + Funding Opportunity</b>\n"
        f"<b>{symbol}</b>\n"
        f"Long: <b>{rec_long.upper()}</b>\n"
        f"Short: <b>{rec_short.upper()}</b>\n"
        f"Mark basis: {mark_basis:+.4f}%\n"
        f"Executable basis: {exec_basis:+.4f}%\n"
        f"Min executable basis: {MIN_EXEC_BASIS_SIGNAL:.2f}%\n"
        f"Funding diff (4h): {fr_diff:+.4f}%\n"
        f"Min funding diff (4h): {min_funding_diff:.4f}%\n"
        f"Reference price: {reference_price:.6f}\n"
        f"Min basis: {min_basis:.2f}%\n"
        f"Bias: {venue_bias}\n"
        f"{funding_note}"
    )


async def telegram_api_request(
    bot_token: str,
    method_name: str,
    *,
    http_method: str = "POST",
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call Telegram Bot API with a certificate fallback for local Windows setups."""
    bot_token = str(bot_token or "").strip()
    url = f"https://api.telegram.org/bot{bot_token}/{method_name}"
    payload = payload or {}
    params = params or {}

    async def request(session: aiohttp.ClientSession, *, verify_ssl: bool = True) -> dict[str, Any]:
        request_kwargs: dict[str, Any] = {
            "ssl": None if verify_ssl else False,
            "timeout": aiohttp.ClientTimeout(total=TELEGRAM_POLL_TIMEOUT_SECONDS + 5),
        }
        if http_method.upper() == "GET":
            request_kwargs["params"] = params
            request_coro = session.get(url, **request_kwargs)
        else:
            request_kwargs["json"] = payload
            request_coro = session.post(url, **request_kwargs)

        async with request_coro as resp:
            try:
                result = await resp.json(content_type=None)
            except Exception:
                result = {"ok": False, "description": await resp.text()}
            ok = bool(result.get("ok", False))
            return {
                "ok": ok,
                "status": resp.status,
                "error_code": result.get("error_code"),
                "description": result.get("description", ""),
                "ssl_fallback": not verify_ssl,
                "result": result.get("result"),
            }

    try:
        async with aiohttp.ClientSession() as session:
            try:
                return await request(session, verify_ssl=True)
            except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError, ssl.SSLError) as cert_error:
                print(f"Telegram SSL verify failed, retrying without SSL verification: {cert_error}")
                details = await request(session, verify_ssl=False)
                if details.get("ok") and not details.get("description"):
                    details["description"] = "Sent using SSL verification fallback"
                return details
    except Exception as exc:
        return {"ok": False, "description": str(exc), "error": type(exc).__name__}


async def send_telegram_message(
    bot_token: str,
    chat_id: str,
    message: str,
    *,
    return_details: bool = False,
) -> dict[str, Any] | bool:
    """Send a message via Telegram bot."""
    payload = {
        "chat_id": str(chat_id or "").strip(),
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    details = await telegram_api_request(bot_token, "sendMessage", payload=payload)
    return details if return_details else bool(details.get("ok"))
