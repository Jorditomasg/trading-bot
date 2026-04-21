import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    # Exchange
    api_key: str = field(default_factory=lambda: os.getenv("BINANCE_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("BINANCE_API_SECRET", ""))
    testnet: bool = field(default_factory=lambda: os.getenv("BINANCE_TESTNET", "true").lower() == "true")

    # Market
    symbol: str = field(default_factory=lambda: os.getenv("SYMBOL", "BTCUSDT"))
    timeframe: str = field(default_factory=lambda: os.getenv("TIMEFRAME", "4h"))

    # Capital & risk
    initial_capital: float = field(default_factory=lambda: float(os.getenv("INITIAL_CAPITAL", "10000")))
    risk_per_trade: float = field(default_factory=lambda: float(os.getenv("RISK_PER_TRADE", "0.01")))

    # Telegram
    telegram_token:   str  = field(default_factory=lambda: os.getenv("TELEGRAM_TOKEN",   ""))
    telegram_chat_id: str  = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    telegram_enabled: bool = field(default_factory=lambda: os.getenv("TELEGRAM_ENABLED", "true").lower() == "true")

    # Misc
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    # Encryption
    fernet_key: str = field(default_factory=lambda: os.getenv("FERNET_KEY", ""))

    def validate(self) -> None:
        if not self.api_key or self.api_key == "your_testnet_api_key":
            raise ValueError("BINANCE_API_KEY is not set in .env")
        if not self.api_secret or self.api_secret == "your_testnet_api_secret":
            raise ValueError("BINANCE_API_SECRET is not set in .env")
        if self.risk_per_trade <= 0 or self.risk_per_trade > 0.05:
            raise ValueError("RISK_PER_TRADE must be between 0 and 0.05 (5%)")
        if self.initial_capital <= 0:
            raise ValueError("INITIAL_CAPITAL must be positive")


settings = Settings()
