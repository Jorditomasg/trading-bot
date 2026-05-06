import logging
import math
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
    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        testnet: bool | None = None,
    ) -> None:
        _api_key    = api_key    if api_key    is not None else settings.api_key
        _api_secret = api_secret if api_secret is not None else settings.api_secret
        _testnet    = testnet    if testnet    is not None else settings.testnet

        kwargs: dict = {}
        if _testnet:
            kwargs["testnet"] = True
            # python-binance respects the testnet flag but we pin the URL explicitly
            self._client = Client(
                _api_key,
                _api_secret,
                **kwargs,
            )
            self._client.API_URL = TESTNET_BASE_URL + "/api"
            logger.info("BinanceClient initialised — TESTNET mode (%s)", TESTNET_BASE_URL)
        else:
            self._client = Client(_api_key, _api_secret)
            logger.info("BinanceClient initialised — LIVE mode")

        self._twm: Optional[ThreadedWebsocketManager] = None

    @_retry
    def get_klines(self, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
        raw = self._client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_asset_volume", "num_trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ])
        # open_time as datetime so chart consumers don't have to derive it
        # synthetically. All numeric callers index by name (open/high/low/close/
        # volume) so the extra column is non-breaking.
        ohlcv = df[["open", "high", "low", "close", "volume"]].astype(float)
        ohlcv.insert(0, "open_time", pd.to_datetime(df["open_time"], unit="ms"))
        logger.debug("Fetched %d klines for %s/%s", len(ohlcv), symbol, interval)
        return ohlcv

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
        order_id = order.get("orderId", "N/A")
        status   = order.get("status", "N/A")
        logger.info("Order placed orderId=%s status=%s", order_id, status)
        return order

    def get_price_precision(self, symbol: str) -> int:
        """Return decimal places for price from PRICE_FILTER → tickSize.

        E.g. BTCUSDT tickSize="0.01" → 2 decimal places.
        Falls back to 2 if the filter is not found.
        """
        info = self._client.get_symbol_info(symbol)
        if info is None:
            logger.warning("Symbol %s not found in exchangeInfo — using price_precision=2", symbol)
            return 2
        for f in info.get("filters", []):
            if f.get("filterType") == "PRICE_FILTER":
                tick = float(f["tickSize"])
                if tick >= 1.0:
                    return 0
                precision = abs(int(math.floor(math.log10(tick))))
                logger.info(
                    "Symbol %s PRICE_FILTER tickSize=%s → price_precision=%d",
                    symbol, f["tickSize"], precision,
                )
                return precision
        logger.warning("PRICE_FILTER not found for %s — using price_precision=2", symbol)
        return 2

    def place_entry_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        entry_price: float,
        price_precision: int = 2,
        wait_seconds: int = 30,
    ) -> dict:
        """Place a LIMIT_MAKER entry order; fall back to MARKET if rejected or unfilled.

        LIMIT_MAKER ensures maker fee (0.02% vs 0.10% taker on Binance Spot).
        The limit price is nudged slightly inside the spread to maximise fill probability.
        If the order is rejected (would be taker) or not filled within *wait_seconds*,
        the order is cancelled and a MARKET order is placed as fallback.

        Returns the Binance order dict (status="FILLED" in all success paths).
        """
        # Nudge 0.01 % inside the spread to stay on the maker side
        if side == "BUY":
            limit_price = round(entry_price * (1 - 0.0001), price_precision)
        else:
            limit_price = round(entry_price * (1 + 0.0001), price_precision)

        price_str = f"{limit_price:.{price_precision}f}"
        logger.info(
            "LIMIT_MAKER entry: symbol=%s side=%s qty=%.5f price=%s (signal=%.2f)",
            symbol, side, quantity, price_str, entry_price,
        )

        try:
            order = self._client.create_order(
                symbol=symbol,
                side=side,
                type=Client.ORDER_TYPE_LIMIT_MAKER,
                quantity=quantity,
                price=price_str,
            )
            order_id = order.get("orderId", "N/A")
            status   = order.get("status", "N/A")
            logger.info(
                "LIMIT_MAKER placed orderId=%s status=%s",
                order_id, status,
            )

            if status == "FILLED":
                return order

            # Poll for fill
            deadline  = time.time() + wait_seconds
            while time.time() < deadline:
                time.sleep(2)
                status_dict = self._client.get_order(symbol=symbol, orderId=order_id)
                current_status = status_dict.get("status", "N/A")
                logger.debug(
                    "LIMIT_MAKER poll orderId=%s status=%s",
                    order_id, current_status,
                )
                if current_status == "FILLED":
                    logger.info(
                        "LIMIT_MAKER filled orderId=%s after %.0fs",
                        order_id, wait_seconds - max(0, deadline - time.time()),
                    )
                    return status_dict
                if current_status in ("CANCELED", "REJECTED", "EXPIRED"):
                    logger.warning(
                        "LIMIT_MAKER order reached terminal state=%s — falling back to MARKET",
                        current_status,
                    )
                    break
            else:
                logger.info(
                    "LIMIT_MAKER not filled within %ds — cancelling, falling back to MARKET",
                    wait_seconds,
                )

            # Cancel unfilled limit order (best-effort)
            try:
                self._client.cancel_order(symbol=symbol, orderId=order_id)
            except Exception as cancel_exc:
                logger.warning("Could not cancel LIMIT_MAKER orderId=%s: %s", order_id, cancel_exc)

        except BinanceAPIException as exc:
            if exc.code == -2010:
                # LIMIT_MAKER rejected because it would have matched immediately as taker
                logger.info(
                    "LIMIT_MAKER rejected (price=%s would be taker) — falling back to MARKET",
                    price_str,
                )
            else:
                logger.warning(
                    "LIMIT_MAKER API error code=%s: %s — falling back to MARKET",
                    exc.code, exc,
                )

        # Fallback: guaranteed-fill market order (pays taker fee)
        logger.info("MARKET fallback: symbol=%s side=%s qty=%.5f", symbol, side, quantity)
        return self.place_order(symbol=symbol, side=side, quantity=quantity)

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

    def get_quantity_precision(self, symbol: str) -> int:
        """Return the number of decimal places for quantity on the given symbol.

        Reads the LOT_SIZE filter from exchangeInfo (unauthenticated endpoint).
        Falls back to 5 if the filter is not found.
        """
        info = self._client.get_symbol_info(symbol)
        if info is None:
            logger.warning("Symbol %s not found in exchangeInfo — using precision=5", symbol)
            return 5
        for f in info.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                step = float(f["stepSize"])
                if step >= 1.0:
                    return 0
                precision = abs(int(math.floor(math.log10(step))))
                logger.info("Symbol %s LOT_SIZE stepSize=%s → precision=%d", symbol, f["stepSize"], precision)
                return precision
        logger.warning("LOT_SIZE filter not found for %s — using precision=5", symbol)
        return 5

    @_retry
    def get_ticker_price(self, symbol: str) -> float:
        ticker = self._client.get_symbol_ticker(symbol=symbol)
        return float(ticker["price"])

    def start_price_stream(
        self, symbol: str, on_tick: Callable[[dict], None]
    ) -> ThreadedWebsocketManager:
        """Start or reuse a single TWM to stream klines for the given symbol."""
        if self._twm is None:
            self._twm = ThreadedWebsocketManager(
                api_key=settings.api_key,
                api_secret=settings.api_secret,
                testnet=settings.testnet,
                tld="com",
            )
            self._twm.start()
            logger.info("ThreadedWebsocketManager started (testnet=%s)", settings.testnet)

        try:
            self._twm.start_kline_socket(callback=on_tick, symbol=symbol, interval="1m")
            logger.info("Price stream added for symbol=%s", symbol)
        except Exception as exc:
            logger.error("Failed to add symbol %s to stream: %s", symbol, exc)

        return self._twm

    def stop_price_stream(self) -> None:
        """Stop the shared ThreadedWebsocketManager."""
        if self._twm:
            try:
                self._twm.stop()
                self._twm = None
                logger.info("ThreadedWebsocketManager stopped.")
            except Exception as exc:
                logger.warning("Error stopping ThreadedWebsocketManager: %s", exc)
