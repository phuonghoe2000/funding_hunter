import urllib.request
import json

pairs = [
    'KATUSDT', 'ONTUSDT', 'ENJUSDT', 'SIRENUSDT', 'PIPPINUSDT', 'TOSHIUSDT',
    'STOUSDT', 'EDGEUSDT', 'XRPUSDT', 'SOLUSDT', 'ETHUSDT', 'BTCUSDT'
]

print(f"{'Pair':<12} {'Binance':>9} {'Asterdex':>9} {'BingX':>9} {'B-A':>9} {'BingX-Aster':>12} Direction")
print("-" * 85)

for sym in pairs:
    results = {}
    
    # Binance
    try:
        url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        results['binance'] = float(data.get('lastFundingRate', 0)) * 100
    except:
        results['binance'] = None
    
    # Asterdex
    try:
        url = f"https://fapi.asterdex.com/fapi/v1/premiumIndex?symbol={sym.lower()}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        results['aster'] = float(data.get('lastFundingRate', 0)) * 100
    except:
        results['aster'] = None
    
    # BingX - correct endpoint
    try:
        bingx_sym = sym.replace('USDT', '-USDT')
        url = f"https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate?symbol={bingx_sym}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        if data.get('code') == 0:
            results['bingx'] = float(data['data'][0].get('lastFundingRate', 0)) * 100
        else:
            results['bingx'] = None
    except:
        results['bingx'] = None
    
    b = results.get('binance')
    a = results.get('aster')
    bx = results.get('bingx')
    
    b_str = f"{b:+.4f}%" if b is not None else "N/A"
    a_str = f"{a:+.4f}%" if a is not None else "N/A"
    bx_str = f"{bx:+.4f}%" if bx is not None else "N/A"
    
    if b is not None and a is not None:
        diff_ba_str = f"{b-a:+.4f}%"
    else:
        diff_ba_str = "N/A"
    
    if bx is not None and a is not None:
        diff_bxa_str = f"{bx-a:+.4f}%"
    elif bx is not None and b is not None:
        diff_bxa_str = f"{bx-b:+.4f}%"
    else:
        diff_bxa_str = "N/A"
    
    direction = "LONG Aster / SHORT Binance" if (b and a and b > a) else "LONG Binance / SHORT Aster" if (b and a) else "N/A"
    
    print(f"{sym:<12} {b_str:>9} {a_str:>9} {bx_str:>9} {diff_ba_str:>9} {diff_bxa_str:>12} {direction}")
