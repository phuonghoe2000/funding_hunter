# 🎯 Funding Hunter

**Công cụ Funding Arbitrage cho OKX, Binance và BingX Futures**

Tool này cho phép bạn mở lệnh futures đồng thời trên 2 trong 3 sàn (OKX, Binance, BingX) để "ăn" funding rate - một chiến lược arbitrage phổ biến trong thị trường crypto.

## ✨ Tính năng

- 📊 **Gửi lệnh Market đồng thời** - Long/Short trên 2 sàn bất kỳ cùng lúc
- 💰 **Theo dõi Funding Rate** - So sánh funding rate của cả 3 sàn
- 🔄 **Đóng lệnh đồng thời** - Đóng position trên cả 2 sàn cùng lúc
- ⚠️ **Auto-close khi liquidation** - Tự động đóng bên còn lại nếu 1 bên bị thanh lý
- 🖥️ **Giao diện GUI** - Dễ sử dụng với tkinter
- 📈 **Theo dõi PnL realtime** - Xem lãi/lỗ của từng position
- 🔀 **Chọn sàn linh hoạt** - Chọn bất kỳ 2 trong 3 sàn để arbitrage

## 🚀 Cài đặt

### 1. Clone repo hoặc copy files

```bash
cd funding_hunter
```

### 2. Cài đặt dependencies

```bash
pip install -r requirements.txt
```

### 3. Chuẩn bị API Keys

Bạn cần tạo API keys trên các sàn muốn dùng (ít nhất 2 sàn):

**OKX:**
1. Vào Account → API → Create API Key
2. Chọn permissions: Trade
3. Lưu lại: API Key, Secret Key, Passphrase

**Binance:**
1. Vào Account → API Management → Create API
2. Enable Futures
3. Lưu lại: API Key, Secret Key

**BingX:**
1. Vào Account → API Management → Create API
2. Enable Futures trading
3. Lưu lại: API Key, Secret Key
4. ⚠️ BingX không có testnet, chỉ trade thật

## 📖 Cách sử dụng

### 1. Chạy ứng dụng

```bash
python main.py
```

### 2. Kết nối

1. Nhập API credentials cho OKX và Binance
2. Chọn Testnet nếu muốn test trước
3. Click "Connect"

### 3. Mở position

1. Chọn trading pair (BTC/USDT, ETH/USDT, ...)
2. Nhập size (số lượng contracts)
3. Chọn leverage
4. **Chọn sàn LONG** (sàn sẽ mở lệnh Long)
5. **Chọn sàn SHORT** (sàn sẽ mở lệnh Short)
6. Click "Open Hedged Position"

### 4. Theo dõi funding

1. Click "Refresh Rates" để xem funding rate hiện tại của cả 3 sàn
2. Tool sẽ tự động tính spread và gợi ý cặp sàn tốt nhất
3. Double-click vào hàng để tự động chọn pair và cặp sàn gợi ý

### 5. Đóng position

- Click "Close All Positions" để đóng tất cả
- Hoặc chọn position trong bảng và click "Close Selected"

## 💡 Chiến lược Funding Arbitrage

### Nguyên lý

Funding rate là khoản thanh toán giữa người Long và người Short mỗi 8 giờ:
- **Funding rate dương**: Long trả tiền cho Short
- **Funding rate âm**: Short trả tiền cho Long

### Cách "ăn" funding

1. **Tìm spread**: So sánh funding rate giữa OKX và Binance
2. **Hedge position**: 
   - Long trên sàn có funding rate thấp hơn (nhận ít/trả ít)
   - Short trên sàn có funding rate cao hơn (nhận nhiều)
3. **Thu lợi nhuận**: Chênh lệch funding rate trừ phí giao dịch

### Ví dụ

```
OKX Funding Rate: 0.01% (Long trả Short)
Binance Funding Rate: 0.03% (Long trả Short)
BingX Funding Rate: 0.02% (Long trả Short)

Chiến lược tốt nhất: Long OKX, Short Binance
- OKX: Trả 0.01%
- Binance: Nhận 0.03%
- Net: +0.02% mỗi 8 giờ
```

## ⚠️ Cảnh báo rủi ro

1. **Slippage**: Giá có thể khác nhau giữa 2 sàn khi mở/đóng lệnh
2. **Liquidation**: Nếu giá di chuyển quá mạnh, 1 bên có thể bị liquidate
3. **Funding rate thay đổi**: Funding rate có thể đảo chiều bất ngờ
4. **API errors**: Có thể xảy ra lỗi khi gửi lệnh
5. **Phí giao dịch**: Cần tính vào phí maker/taker của cả 2 sàn

## 🔧 Cấu trúc project

```
funding_hunter/
├── config/
│   ├── __init__.py
│   ├── settings.py      # Cấu hình API (OKX, Binance, BingX)
│   └── constants.py     # Constants và enums
├── exchanges/
│   ├── __init__.py
│   ├── base.py          # Base client interface
│   ├── okx_client.py    # OKX API client
│   ├── binance_client.py # Binance API client
│   └── bingx_client.py  # BingX API client
├── core/
│   ├── __init__.py
│   ├── position_manager.py  # Quản lý positions
│   └── funding_monitor.py   # Theo dõi funding rates
├── gui/
│   ├── __init__.py
│   └── app.py           # GUI application
├── main.py              # Entry point
├── requirements.txt
└── README.md
```

## 📝 Notes

- Tool này dùng **Hedge Mode** (có thể giữ cả Long và Short cùng lúc)
- Mặc định dùng **Cross Margin**
- OKX và Binance hỗ trợ cả Mainnet và Testnet
- **BingX chỉ có Mainnet** - cẩn thận khi test
- Cần kết nối ít nhất 2 sàn để trade

## ⚖️ Disclaimer

Tool này chỉ dùng cho mục đích giáo dục và nghiên cứu. Trading cryptocurrency có rủi ro cao và có thể mất toàn bộ vốn. Tác giả không chịu trách nhiệm cho bất kỳ tổn thất nào từ việc sử dụng tool này.

**USE AT YOUR OWN RISK!**
