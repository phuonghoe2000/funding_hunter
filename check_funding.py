import urllib.request
import json

pairs = ['KATUSDT', 'ONTUSDT', 'ENJUSDT', 'SIRENUSDT', 'PIPPINUSDT', 'TOSHIUSDT']

print(f"{'Pair':<14} {'Binance FR':>10} {'Aster FR':>10} {'Diff (B-A)':>10} {'Direction':<30}")
print("-" * 80)
for sym in pairs:
    try:
        b_url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}"
        req = urllib.request.Request(b_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            b_data = json.loads(resp.read())
        b_fr = float(b_data.get('lastFundingRate', 0)) * 100
    except Exception as e:
        b_fr = None
    
    try:
        a_url = f"https://fapi.asterdex.com/fapi/v1/premiumIndex?symbol={sym.lower()}"
        req = urllib.request.Request(a_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            a_data = json.loads(resp.read())
        a_fr = float(a_data.get('lastFundingRate', 0)) * 100
    except Exception as e:
        a_fr = None
    
    if b_fr is not None and a_fr is not None:
        diff = b_fr - a_fr
        direction = "LONG Aster / SHORT Binance" if diff > 0 else "LONG Binance / SHORT Aster"
        diff_str = f"{diff:+.4f}%"
        b_str = f"{b_fr:+.4f}%"
        a_str = f"{a_fr:+.4f}%"
    else:
        direction = "N/A"
        diff_str = "N/A"
        b_str = f"{b_fr}" if b_fr is not None else "N/A"
        a_str = f"{a_fr}" if a_fr is not None else "N/A"
    
    print(f"{sym:<14} {b_str:>10} {a_str:>10} {diff_str:>10}   {direction}")
