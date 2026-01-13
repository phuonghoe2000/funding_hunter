"""
GUI module
"""
# Lazy imports to avoid circular import when running as module
__all__ = ["FundingHunterGUI", "main"]

def __getattr__(name):
    if name == "FundingHunterGUI":
        from .app import FundingHunterGUI
        return FundingHunterGUI
    elif name == "main":
        from .app import main
        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
