import subprocess
import json
import sys
import os

def run_cmd(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.stdout

os.chdir(r'D:\MMO\FUNDING\funding_hunter')
print("--- ANKR HEDGE STATUS ---")
print(run_cmd('python cli.py positions'))
print("\n--- PNL REPORT ---")
print(run_cmd('python cli.py pnl --pair ANKR/USDT --long binance --short bingx'))
