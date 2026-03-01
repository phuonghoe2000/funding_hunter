import urllib.request
import json
import os
import sys

def download_klines(symbol, interval, limit, filename):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol.upper()}&interval={interval}&limit={limit}"
    
    print(f"Downloading {limit} candles for {symbol} ({interval})...")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            with open(filename, 'w') as f:
                json.dump(data, f)
            print(f"Saved {len(data)} candles to {filename}")
    except Exception as e:
        print(f"Error downloading data: {e}")

if __name__ == "__main__":
    # We download 100 candles. 10 hours * 4 (15m periods/hour) = 40 candles.
    # We need extra candles (~30) for lookback periods to "warm up" the indicators.
    download_klines("POWERUSDT", "15m", 150, "powerusdt_15m_recent.json")
