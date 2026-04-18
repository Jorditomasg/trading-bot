import logging
import time
from typing import Callable, Optional

import pandas as pd
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from binance import ThreadedWebsocketManager

from bot.config import settings

logger = logging.getLogger(__name__)

TESTNET_BASE_URL = "https://testnet.binance.vision"
MAX_RETRIES = 3
BACKOFF_BASE = 2.0  # seconds


def _retry(func):
    """Decorator: retry up to MAX_RETRIES times with exponential backoff."""
    def wrapper(*args, **kwargs):
        last_exc: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except (BinanceAPIException, BinanceRequestException, Exception) as exc:
                last_exc = exc
                wait = BACKOFF_BASE ** attempt
                logger.warning(
                    "Attempt %d/%d failed for %s: %s — retrying in %.1fs",
                    attempt, MAX_RETRIES, func.__name__, exc, wait,
                )
                time.sleep(wait)
        raise RuntimeError(
            f"All {MAX_RETRIES} attempts failed for {func.__name__}"
        ) from last_exc
    wrapper.__name__ = func.__name__
    return wrapper


class BinanceClient:
    def __init__(self) -> None:
        kwargs: dict = {}
        if settings.testnet:
            kwargs["testnet"] = True
            # python-binance respects the testnet flag but we pin the URL explicitly
            self._client = Client(
                settings.api_key,
                settings.api_secret,
                **kwargs,
            )
            self._client.API_URL = TESTNET_BASE_URL + "/api"
            logger.info("BinanceClient initialised — TESTNET mode (%s)", TESTNET_BASE_URL)
        else:
            self._client = Client(settings.api_key, settings.api_secret)
            logger.info("BinanceClient initialised — LIVE mode")

    @_retry
    def get_klines(self, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
        raw = self._client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_asset_volume", "num_trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ])
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        logger.debug("Fetched %d klines for %s/%s", len(df), symbol, interval)
        return df

    @_retry
    def get_balance(self, asset: str = "USDT") -> float:
        account = self._client.get_account()
        for bal in account["balances"]:
            if bal["asset"] == asset:
                value = float(bal["free"])
                logger.debug("Balance %s = %.4f", asset, value)
                return value
        logger.warning("Asset %s not found in account balances", asset)
        return 0.0

    @_retry
    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = Client.ORDER_TYPE_MARKET,
    ) -> dict:
        logger.info(
            "Placing order symbol=%s side=%s qty=%.5f type=%s",
            symbol, side, quantity, order_type,
        )
        order = self._client.create_order(
            symbol=symbol,
            side=side,
            type=order_type,
            quantity=quantity,
        )
        logger.info("Order placed orderId=%s status=%s", order["orderId"], order["status"])
        return order

    @_retry
    def get_open_orders(self, symbol: str) -> list:
        orders = self._client.get_open_orders(symbol=symbol)
        logger.debug("Open orders for %s: %d", symbol, len(orders))
        return orders

    @_retry
    def cancel_order(self, symbol: str, order_id: int) -> dict:
        result = self._client.cancel_order(symbol=symbol, orderId=order_id)
        logger.info("Order cancelled symbol=%s orderId=%s", symbol, order_id)
        return result

    def start_price_stream(
        self, symbol: str, on_tick: Callable[[dict], None]
    ) -> ThreadedWebsocketManager:
        twm = ThreadedWebsocketManager(
            api_key=settings.api_key,
            api_secret=settings.api_secret,
            testnet=settings.testnet,
            tld="com",
        )
        twm.start()
        twm.start_kline_socket(callback=on_tick, symbol=symbol, interval="1m")
        logger.info(
            "Price stream started symbol=%s testnet=%s", symbol, settings.testnet
        )
        return twm
