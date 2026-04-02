import os
import json
import logging
from config.settings import Settings, OKXConfig, BinanceConfig, BingXConfig, GateConfig, AsterdexConfig, BybitConfig

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency at runtime
    load_dotenv = None

logger = logging.getLogger(__name__)

class ConfigManager:
    """Manages loading API configurations from user_config.json for the CLI."""
    
    def __init__(self, config_file: str = "user_config.json"):
        # Support both root config and dist/ config for backward compatibility
        if os.path.exists(config_file):
            self.config_path = config_file
        elif os.path.exists(os.path.join("dist", config_file)):
            self.config_path = os.path.join("dist", config_file)
        else:
            self.config_path = config_file

    def get_settings(self) -> Settings:
        """Loads user_config.json and builds a complete Settings object."""
        settings = Settings()
        if load_dotenv:
            load_dotenv()
        
        try:
            if not os.path.exists(self.config_path):
                settings.load_from_env()
                configured = [
                    cfg.api_key for cfg in [
                        settings.okx,
                        settings.binance,
                        settings.bingx,
                        settings.gate,
                        settings.asterdex,
                        settings.bybit,
                    ]
                ]
                if any(configured):
                    logger.info(f"Config file {self.config_path} not found. Loaded settings from environment variables.")
                else:
                    logger.warning(f"Config file {self.config_path} not found. Using empty settings.")
                return settings
            
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config_data = json.load(f)
                
            # OKX
            if "okx" in config_data and config_data["okx"].get("enabled", False):
                settings.okx = OKXConfig(
                    api_key=config_data["okx"].get("api_key", ""),
                    secret_key=config_data["okx"].get("secret", ""),
                    passphrase=config_data["okx"].get("passphrase", ""),
                    testnet=config_data["okx"].get("testnet", False)
                )
                
            # Binance
            if "binance" in config_data and config_data["binance"].get("enabled", False):
                settings.binance = BinanceConfig(
                    api_key=config_data["binance"].get("api_key", ""),
                    secret_key=config_data["binance"].get("secret", ""),
                    testnet=config_data["binance"].get("testnet", False)
                )

            # BingX
            if "bingx" in config_data and config_data["bingx"].get("enabled", False):
                settings.bingx = BingXConfig(
                    api_key=config_data["bingx"].get("api_key", ""),
                    secret_key=config_data["bingx"].get("secret", "")
                )

            # Gate
            if "gate" in config_data and config_data["gate"].get("enabled", False):
                settings.gate = GateConfig(
                    api_key=config_data["gate"].get("api_key", ""),
                    secret_key=config_data["gate"].get("secret", "")
                )
                
            # Asterdex
            if "asterdex" in config_data and config_data["asterdex"].get("enabled", False):
                settings.asterdex = AsterdexConfig(
                    api_key=config_data["asterdex"].get("api_key", ""),
                    secret_key=config_data["asterdex"].get("secret", "")
                )

            # Bybit
            if "bybit" in config_data and config_data["bybit"].get("enabled", False):
                settings.bybit = BybitConfig(
                    api_key=config_data["bybit"].get("api_key", ""),
                    secret_key=config_data["bybit"].get("secret", ""),
                    testnet=config_data["bybit"].get("testnet", False)
                )
                
            logger.info(f"Successfully loaded configuration from {self.config_path}")
                
        except Exception as e:
            logger.error(f"Failed to load config from {self.config_path}: {e}")
            settings.load_from_env()
            
        return settings
