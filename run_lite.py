#!/usr/bin/env python3
"""
Launcher for Funding Hunter LITE version
"""
import sys
import os

# Add project root to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import and run
from gui.app_lite import main

if __name__ == "__main__":
    main()
