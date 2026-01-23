"""
Configuration settings for Funding Hunter
"""
import os
from dataclasses import dataclass
from typing import Optional

@dataclass
class OKXConfig:
    """OKX API Configuration"""
    api_key: str = ""
    secret_key: str = ""
    passphrase: str = ""
    # True for testnet, False for mainnet
    testnet: bool = True
    
    # API endpoints
    @property
    def base_url(self) -> str:
        if self.testnet:
            return "https://www.okx.com"  # OKX uses same URL with different flag
        return "https://www.okx.com"
    
    @property
    def ws_url(self) -> str:
        if self.testnet:
            return "wss://wspap.okx.com:8443/ws/v5/private?brokerId=9999"
        return "wss://ws.okx.com:8443/ws/v5/private"


@dataclass
class BinanceConfig:
    """Binance API Configuration"""
    api_key: str = ""
    secret_key: str = ""
    # True for testnet, False for mainnet
    testnet: bool = True
    
    @property
    def base_url(self) -> str:
        if self.testnet:
            return "https://testnet.binancefuture.com"
        return "https://fapi.binance.com"
    
    @property
    def ws_url(self) -> str:
        if self.testnet:
            return "wss://stream.binancefuture.com"
        return "wss://fstream.binance.com"


@dataclass
class BingXConfig:
    """BingX API Configuration"""
    api_key: str = ""
    secret_key: str = ""
    # BingX doesn't have testnet for perpetual futures
    testnet: bool = False
    
    @property
    def base_url(self) -> str:
        return "https://open-api.bingx.com"
    
    @property
    def ws_url(self) -> str:
        return "wss://open-api-swap.bingx.com/swap-market"


@dataclass
class BybitConfig:
    """Bybit API Configuration"""
    api_key: str = ""
    secret_key: str = ""
    # True for testnet, False for mainnet
    testnet: bool = False
    
    @property
    def base_url(self) -> str:
        if self.testnet:
            return "https://api-testnet.bybit.com"
        return "https://api.bybit.com"
    
    @property
    def ws_url(self) -> str:
        if self.testnet:
            return "wss://stream-testnet.bybit.com/v5/private"
        return "wss://stream.bybit.com/v5/private"


@dataclass
class TradingConfig:
    """Trading parameters"""
    # Default leverage
    default_leverage: int = 10
    
    # Position monitoring interval (seconds)
    monitor_interval: float = 1.0
    
    # Funding rate check interval (seconds)
    funding_check_interval: float = 60.0
    
    # Maximum slippage allowed (percentage)
    max_slippage: float = 0.1
    
    # Auto close if one side liquidated
    auto_close_on_liquidation: bool = True
    
    # Maximum retries for API calls
    max_retries: int = 3
    
    # Timeout for API calls (seconds)
    api_timeout: int = 10


class Settings:
    """Main settings class"""
    
    def __init__(self):
        self.okx = OKXConfig()
        self.binance = BinanceConfig()
        self.bingx = BingXConfig()
        self.bybit = BybitConfig()
        self.trading = TradingConfig()
    
    def load_from_env(self):
        """Load settings from environment variables"""
        # OKX
        self.okx.api_key = os.getenv("OKX_API_KEY", "")
        self.okx.secret_key = os.getenv("OKX_SECRET_KEY", "")
        self.okx.passphrase = os.getenv("OKX_PASSPHRASE", "")
        self.okx.testnet = os.getenv("OKX_TESTNET", "true").lower() == "true"
        
        # Binance
        self.binance.api_key = os.getenv("BINANCE_API_KEY", "")
        self.binance.secret_key = os.getenv("BINANCE_SECRET_KEY", "")
        self.binance.testnet = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
        
        # BingX
        self.bingx.api_key = os.getenv("BINGX_API_KEY", "")
        self.bingx.secret_key = os.getenv("BINGX_SECRET_KEY", "")
        
        # Bybit
        self.bybit.api_key = os.getenv("BYBIT_API_KEY", "")
        self.bybit.secret_key = os.getenv("BYBIT_SECRET_KEY", "")
        self.bybit.testnet = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
        
        # Trading
        self.trading.default_leverage = int(os.getenv("DEFAULT_LEVERAGE", "10"))
        self.trading.auto_close_on_liquidation = os.getenv("AUTO_CLOSE_ON_LIQUIDATION", "true").lower() == "true"
    
    def validate(self) -> tuple[bool, str]:
        """Validate settings"""
        errors = []
        
        if not self.okx.api_key:
            errors.append("OKX API Key is required")
        if not self.okx.secret_key:
            errors.append("OKX Secret Key is required")
        if not self.okx.passphrase:
            errors.append("OKX Passphrase is required")
            
        if not self.binance.api_key:
            errors.append("Binance API Key is required")
        if not self.binance.secret_key:
            errors.append("Binance Secret Key is required")
        
        if errors:
            return False, "\n".join(errors)
        return True, "Settings valid"


# Global settings instance
settings = Settings()
