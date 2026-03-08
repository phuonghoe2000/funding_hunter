"""
Auto Config Scanner

Tự động quét nhiều bộ config bằng cách chạy backtest trên dữ liệu 15m candle
download từ Binance, sau đó chọn ra bộ config tốt nhất để trade thật.

Flow:
1. Download 1000 candles 15m từ Binance Futures API
2. Chạy backtest grid search trên nhiều bộ config
3. Tính điểm (score) cho mỗi bộ config
4. Chọn bộ config có score cao nhất
5. Apply vào GUI → bắt đầu trade thật
"""

import urllib.request
import json
import sys
import os
from typing import Optional, Callable, List
from dataclasses import dataclass

# Ensure project root is in path for importing backtest
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from backtest import BacktestConfig, BacktestEngine, BacktestTrade


@dataclass
class ScanResult:
    """Result of a single config backtest scan"""
    config: BacktestConfig
    total_pnl: float
    total_pnl_pct: float
    num_trades: int
    win_rate: float
    max_drawdown: float
    avg_pnl_per_trade: float
    profit_factor: float
    max_consec_losses: int
    score: float


def download_klines_data(symbol: str, interval: str = "15m", limit: int = 1000,
                         on_log: Optional[Callable] = None) -> Optional[list]:
    """Download klines data from Binance Futures API
    
    Args:
        symbol: Trading pair like "POWER/USDT" or "POWERUSDT"
        interval: Candle interval (default "15m")
        limit: Number of candles to download (default 1000)
        on_log: Optional callback for log messages
    
    Returns:
        List of kline data, or None on failure
    """
    log = on_log or print
    
    # Clean symbol: "POWER/USDT" -> "POWERUSDT"
    clean_symbol = symbol.replace("/", "").replace("-", "").upper()
    
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={clean_symbol}&interval={interval}&limit={limit}"
    
    log(f"📥 Downloading {limit} candles for {clean_symbol} ({interval})...")
    
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode('utf-8'))
            
            # Check for API error response
            if isinstance(data, dict) and 'code' in data:
                log(f"❌ Binance API error: {data.get('msg', 'Unknown error')}")
                return None
            
            if not isinstance(data, list) or len(data) == 0:
                log(f"❌ No data returned from Binance")
                return None
            
            log(f"✅ Downloaded {len(data)} candles successfully")
            return data
    except urllib.error.HTTPError as e:
        if e.code == 400:
            log(f"❌ Invalid symbol or parameters: {clean_symbol}")
        else:
            log(f"❌ HTTP Error {e.code}: {e.reason}")
        return None
    except urllib.error.URLError as e:
        log(f"❌ Network error: {e.reason}")
        return None
    except Exception as e:
        log(f"❌ Error downloading data: {e}")
        return None


def run_config_scan(klines: list, volume_usdt: float = 15.0, leverage: int = 5,
                    on_log: Optional[Callable] = None,
                    on_progress: Optional[Callable] = None) -> List[ScanResult]:
    """Run backtest grid search over multiple config combinations
    
    Tests different combinations of:
    - price_change_threshold: Ngưỡng % thay đổi giá để trigger signal
    - lookback_candles: Số nến nhìn lại
    - take_profit_pct: % chốt lời
    - stop_loss_pct: % cắt lỗ
    - volume_multiplier: Hệ số volume xác nhận
    - trailing TP settings: Trailing take profit
    
    Args:
        klines: Raw kline data from Binance
        volume_usdt: Position volume in USDT
        leverage: Trading leverage
        on_log: Optional callback for log messages
        on_progress: Optional callback (current, total) for progress updates
    
    Returns:
        List of ScanResult sorted by score (best first)
    """
    log = on_log or print
    
    # ==================== Parameter Grid ====================
    # Giữ gọn 3-4 mốc/tham số để tránh overfitting trên 1000 nến (~10 ngày)
    thresholds = [0.5, 1.0, 1.5]
    lookbacks = [5, 7, 10]
    tps = [0.5, 1.0, 2.0, 3.5]
    sls = [0.3, 0.5, 1.0]
    vol_multipliers = [1.5, 2.0]
    trailing_configs = [
        # (use_trailing, tp_extension_pct, max_tp_extensions)
        (True, 0.3, 5),
        (False, 0.0, 0),
    ]
    
    total = (len(thresholds) * len(lookbacks) * len(tps) * len(sls) 
             * len(vol_multipliers) * len(trailing_configs))
    log(f"🔍 Scanning {total} config combinations...")
    
    results: List[ScanResult] = []
    count = 0
    
    for threshold in thresholds:
        for lookback in lookbacks:
            for tp in tps:
                for sl in sls:
                    for vol_mult in vol_multipliers:
                        for use_trailing, tp_ext, max_tp in trailing_configs:
                            config = BacktestConfig(
                                price_change_threshold=threshold,
                                lookback_candles=lookback,
                                position_volume_usdt=volume_usdt,
                                leverage=leverage,
                                take_profit_pct=tp,
                                stop_loss_pct=sl,
                                use_trailing_tp=use_trailing,
                                tp_extension_pct=tp_ext,
                                max_tp_extensions=max_tp,
                                use_volume_confirmation=True,
                                volume_multiplier=vol_mult,
                                volume_lookback=20,
                                use_rsi_filter=True,
                                rsi_period=14,
                                rsi_overbought=70.0,
                                rsi_oversold=30.0,
                                cooldown_candles=4
                            )
                            
                            engine = BacktestEngine(config, silent=True)
                            trades = engine.run(klines)
                            
                            result = _calculate_metrics(config, trades)
                            results.append(result)
                            
                            count += 1
                            if on_progress and count % 100 == 0:
                                on_progress(count, total)
    
    # Sort by score (best first)
    results.sort(key=lambda r: r.score, reverse=True)
    
    # Count profitable configs
    profitable = sum(1 for r in results if r.total_pnl > 0 and r.num_trades >= 3)
    log(f"✅ Scan complete! Tested {count} configs | {profitable} profitable configs found")
    
    return results


def _calculate_metrics(config: BacktestConfig, trades: List[BacktestTrade]) -> ScanResult:
    """Calculate performance metrics and composite score for ranking"""
    if not trades:
        return ScanResult(
            config=config, total_pnl=0, total_pnl_pct=0, num_trades=0,
            win_rate=0, max_drawdown=0, avg_pnl_per_trade=0,
            profit_factor=0, max_consec_losses=0, score=-999
        )
    
    wins = [t for t in trades if t.pnl_pct > 0]
    total_pnl = sum(t.pnl_usdt for t in trades)
    total_pnl_pct = sum(t.pnl_pct for t in trades)
    win_rate = len(wins) / len(trades) * 100
    avg_pnl = total_pnl / len(trades)
    
    # Profit factor = gross_profit / gross_loss
    gross_profit = sum(t.pnl_usdt for t in trades if t.pnl_usdt > 0)
    gross_loss = abs(sum(t.pnl_usdt for t in trades if t.pnl_usdt < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (10.0 if gross_profit > 0 else 0)
    
    # Max drawdown
    equity = 0
    peak = 0
    max_dd = 0
    for t in trades:
        equity += t.pnl_usdt
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)
    
    # Max consecutive losses
    max_consec = 0
    current = 0
    for t in trades:
        if t.pnl_pct <= 0:
            current += 1
            max_consec = max(max_consec, current)
        else:
            current = 0
    
    # ==================== Composite Score ====================
    # Tính Drawdown theo % vốn (position_volume * leverage) để scale đúng mọi volume
    effective_capital = config.position_volume_usdt * config.leverage
    max_dd_pct = (max_dd / effective_capital * 100) if effective_capital > 0 else 0
    
    score = 0.0
    
    # 1. Total PnL (most important - trọng số cao nhất)
    score += total_pnl * 3.0
    
    # 2. Win rate bonus (trên 50% là tốt)
    if win_rate >= 60:
        score += (win_rate - 50) * 0.8
    elif win_rate >= 50:
        score += (win_rate - 50) * 0.4
    
    # 3. Profit factor bonus (PF >= 2 là rất tốt)
    if profit_factor >= 2.0:
        score += profit_factor * 2.0
    elif profit_factor >= 1.5:
        score += profit_factor * 1.0
    
    # 4. Penalize quá ít trades (không đủ thống kê)
    # FIX: Chỉ nhân nhỏ khi score > 0. Nếu score < 0 thì ít lệnh = phạt nặng hơn.
    if len(trades) < 3:
        if score > 0:
            score *= 0.2
        else:
            score *= 5.0  # Âm × 5 = âm nặng hơn
    elif len(trades) < 5:
        if score > 0:
            score *= 0.5
        else:
            score *= 2.0
    elif len(trades) < 8:
        if score > 0:
            score *= 0.8
        else:
            score *= 1.2
    
    # 5. Penalize drawdown cao (theo % vốn, scale chuẩn mọi volume)
    if max_dd_pct > 0:
        score -= max_dd_pct * 2.0
    
    # 6. Penalize chuỗi thua liên tục
    if max_consec >= 5:
        score -= max_consec * 0.5
    
    return ScanResult(
        config=config,
        total_pnl=total_pnl,
        total_pnl_pct=total_pnl_pct,
        num_trades=len(trades),
        win_rate=win_rate,
        max_drawdown=max_dd,
        avg_pnl_per_trade=avg_pnl,
        profit_factor=profit_factor,
        max_consec_losses=max_consec,
        score=score
    )


def get_best_config(results: List[ScanResult], min_trades: int = 3) -> Optional[ScanResult]:
    """Get the best config from scan results
    
    Args:
        results: List of ScanResult sorted by score (best first)
        min_trades: Minimum number of trades required
    
    Returns:
        Best ScanResult or None if no valid result found
    """
    # Priority 1: profitable configs with enough trades
    valid = [r for r in results if r.num_trades >= min_trades and r.total_pnl > 0]
    
    if not valid:
        # Priority 2: any profitable config
        valid = [r for r in results if r.num_trades > 0 and r.total_pnl > 0]
    
    if not valid:
        # Priority 3: any config with trades (even if losing)
        valid = [r for r in results if r.num_trades > 0]
    
    return valid[0] if valid else None


def format_top_results(results: List[ScanResult], top_n: int = 5) -> str:
    """Format top N results for display in GUI log
    
    Args:
        results: List of ScanResult sorted by score
        top_n: Number of top results to show
    
    Returns:
        Formatted string with top results
    """
    lines = []
    lines.append(f"🏆 Top {top_n} Configs:")
    lines.append("─" * 75)
    
    valid = [r for r in results if r.num_trades > 0][:top_n]
    
    for i, r in enumerate(valid, 1):
        c = r.config
        trailing_str = (f"Trailing ON (+{c.tp_extension_pct}%, max {c.max_tp_extensions})" 
                       if c.use_trailing_tp else "Trailing OFF")
        
        pnl_emoji = "✅" if r.total_pnl > 0 else "❌"
        
        lines.append(
            f"  #{i} {pnl_emoji} PnL: {r.total_pnl:+.2f}$ | WR: {r.win_rate:.0f}% | "
            f"Trades: {r.num_trades} | DD: {r.max_drawdown:.2f}$ | "
            f"PF: {r.profit_factor:.2f} | Score: {r.score:.1f}"
        )
        lines.append(
            f"      Threshold: {c.price_change_threshold}%/{c.lookback_candles}c | "
            f"TP: {c.take_profit_pct}% | SL: {c.stop_loss_pct}% | "
            f"Vol: {c.volume_multiplier}x | {trailing_str}"
        )
    
    if not valid:
        lines.append("  ❌ Không tìm thấy config có lợi nhuận")
    
    lines.append("─" * 75)
    return "\n".join(lines)
