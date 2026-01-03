#!/usr/bin/env python3
"""
Funding Hunter - Funding Arbitrage Tool
Trade futures on OKX and Binance simultaneously to capture funding rate differences.

Usage:
    python main.py
"""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.app import main

if __name__ == "__main__":
    main()
