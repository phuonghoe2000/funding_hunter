import urllib.request, json, time as _t
from datetime import datetime, timezone

def fetch_batch(symbol, interval, start_ms, limit=1500):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&startTime={start_ms}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())

symbol   = "ETHUSDT"
interval = "15m"
target   = 10000
batch    = 1500

end_ms   = int(_t.time() * 1000)
span_ms  = target * 15 * 60 * 1000
start_ms = end_ms - span_ms

all_klines = []
current_start = start_ms
print(f"Downloading {target} candles for {symbol} {interval}...")

while len(all_klines) < target:
    batch_data = fetch_batch(symbol, interval, current_start, batch)
    if not batch_data:
        break
    all_klines.extend(batch_data)
    t0 = datetime.fromtimestamp(batch_data[0][0]/1000, tz=timezone.utc).strftime("%m/%d %H:%M")
    t1 = datetime.fromtimestamp(batch_data[-1][6]/1000, tz=timezone.utc).strftime("%m/%d %H:%M")
    print(f"  +{len(batch_data):>4} candles ({t0} -> {t1})  total={len(all_klines)}")
    current_start = batch_data[-1][6] + 1
    if len(batch_data) < batch:
        break
    _t.sleep(0.2)

all_klines = all_klines[:target]
out = "ethusdt_15m_10000.json"
with open(out, "w") as f:
    json.dump(all_klines, f)

t_start = datetime.fromtimestamp(all_klines[0][0]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
t_end   = datetime.fromtimestamp(all_klines[-1][6]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
print(f"\nSaved {len(all_klines)} candles to {out}")
print(f"Range: {t_start} -> {t_end} UTC")
