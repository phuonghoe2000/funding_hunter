"""
Auto Trade Demo - Main Entry Point

Run this file to start the auto trade demo application.
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.autotrade_gui import main

if __name__ == "__main__":
    main()
