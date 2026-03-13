# 🎯 Funding Hunter

**Multi-Exchange Funding Rate Arbitrage Tool** — Hỗ trợ OKX, Binance, BingX, Gate.io, Asterdex

Tool cho phép mở lệnh futures đồng thời trên 2 sàn để "ăn" chênh lệch funding rate. Hỗ trợ cả **GUI** (tkinter) và **CLI** (command line).

## ✨ Tính năng

- 📊 **Scan funding rates** — So sánh rates chuẩn hoá 4H trên tất cả sàn
- 🚀 **Mở lệnh hedge** — Analyze spread + DCA splits trên 2 sàn đồng thời
- 🛑 **Đóng lệnh hedge** — Analyze spread + split close thông minh
- 👁 **Monitor realtime** — Theo dõi PnL, Risk%, funding fees liên tục
- 🔄 **Auto-close** — Tự đóng khi risk vượt ngưỡng hoặc funding đảo chiều
- 📥 **Load positions** — Tự detect lệnh hedge đang mở trên các sàn
- 💰 **PnL Report** — Tính lãi/lỗ chi tiết (realized PnL + commission + funding fees)
- 📈 **Pair info** — Giá, order book, funding rate cho bất kỳ pair nào
- 🤖 **CLI cho AI Agent** — Toàn bộ tính năng qua command line

## 🚀 Cài đặt

```bash
cd funding_hunter
pip install -r requirements.txt
```

### Chuẩn bị API Keys

Tạo API keys trên ít nhất 2 sàn:

| Sàn | Cần | Lưu ý |
|-----|-----|-------|
| **OKX** | API Key, Secret, Passphrase | Cần permission Trade |
| **Binance** | API Key, Secret | Enable Futures |
| **BingX** | API Key, Secret | ⚠️ Không có testnet |
| **Gate.io** | API Key, Secret | Enable Futures |
| **Asterdex** | API Key, Secret | — |

Cấu hình API keys trong file `user_config.json` (hoặc `dist/user_config.json`).

## 📖 Sử dụng

### GUI Mode

```bash
python main.py
```

### CLI Mode

```bash
# Scan cơ hội funding rate
python cli.py scan [--min-spread 0.001] [--top 15]

# Xem balance & positions
python cli.py status

# Detect lệnh hedge đang mở
python cli.py positions

# Xem chi tiết pair (giá, funding, orderbook)
python cli.py info --pair BTC/USDT --long binance --short bingx

# Mở lệnh hedge (analyze spread → DCA entry)
python cli.py open --pair BTC/USDT --long gate --short binance \
    --size 100 --leverage 3 [--splits 5] [--skip-leverage]

# Đóng lệnh hedge (analyze spread → split close)
python cli.py close --pair BTC/USDT --long gate --short binance [--splits 3]

# Monitor PnL realtime + auto-close
python cli.py monitor --pair BTC/USDT --long gate --short binance \
    [--auto-close-risk 10] [--auto-close-reversal] [--interval 5]

# Tính PnL từ trade history
python cli.py pnl --pair BTC/USDT --long gate --short binance \
    [--since "2026-03-12 10:00:00"]

# Analyze price spread (open/close mode)
python cli.py analyze --pair BTC/USDT --long gate --short binance \
    [--duration 120] [--mode close]
```

## 💡 Chiến lược Funding Arbitrage

### Nguyên lý

Funding rate là khoản thanh toán giữa Long và Short định kỳ (1h/4h/8h tuỳ sàn):
- **Rate dương**: Long trả Short
- **Rate âm**: Short trả Long

### Cách "ăn" funding

1. **Scan spread**: `python cli.py scan` — tìm pair có chênh lệch lớn nhất
2. **Hedge**: Long sàn có rate thấp, Short sàn có rate cao
3. **Monitor**: Theo dõi PnL + auto-close khi funding đảo chiều
4. **Thu lợi**: Chênh lệch funding rate trừ phí giao dịch

```
Ví dụ:
  Binance Funding Rate: 0.03%   →  Short Binance (nhận 0.03%)
  Gate.io Funding Rate: 0.01%   →  Long Gate (trả 0.01%)
  Net: +0.02% mỗi funding period
```

## ⚠️ Cảnh báo rủi ro

1. **Slippage** — Giá khác nhau giữa 2 sàn khi mở/đóng lệnh
2. **Liquidation** — Giá di chuyển quá mạnh có thể liquidate 1 bên
3. **Funding reversal** — Funding rate có thể đảo chiều bất ngờ
4. **API errors** — Lỗi kết nối, rate limit từ sàn
5. **Phí giao dịch** — Cần tính phí maker/taker cả 2 sàn

## 🔧 Cấu trúc project

```
funding_hunter/
├── config/
│   ├── settings.py          # Cấu hình API (OKX, Binance, BingX, Gate, Aster)
│   └── constants.py         # Constants, enums, helper functions
├── exchanges/
│   ├── base.py              # Base client interface
│   ├── okx_client.py        # OKX API client
│   ├── binance_client.py    # Binance API client
│   ├── bingx_client.py      # BingX API client
│   ├── gate_client.py       # Gate.io API client
│   └── aster_client.py      # Asterdex API client
├── core/
│   ├── config_manager.py    # Load config không cần GUI
│   ├── multi_exchange.py    # Quản lý multi-exchange (decoupled)
│   ├── trading_engine.py    # Core trading logic cho CLI
│   ├── position_manager.py  # Quản lý positions
│   └── funding_monitor.py   # Theo dõi funding rates
├── gui/
│   └── app.py               # GUI application (tkinter)
├── cli.py                   # ⭐ CLI entry point (9 subcommands)
├── main.py                  # GUI entry point
├── requirements.txt
└── README.md
```

## 📝 Notes

- Mặc định dùng **Cross Margin**
- OKX và Binance hỗ trợ cả Mainnet và Testnet
- **BingX chỉ có Mainnet** — cẩn thận khi test
- Funding rates được chuẩn hoá về **4H** để so sánh công bằng giữa các sàn
- CLI hỗ trợ đầy đủ tính năng cho **AI Agent** tự động trade

## ⚖️ Disclaimer

Tool này chỉ dùng cho mục đích giáo dục và nghiên cứu. Trading cryptocurrency có rủi ro cao và có thể mất toàn bộ vốn. Tác giả không chịu trách nhiệm cho bất kỳ tổn thất nào.

**USE AT YOUR OWN RISK!**
