import logging
import math
import time
import uuid
from decimal import ROUND_DOWN, Decimal
from typing import Callable, Optional

import pandas as pd
import requests
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from binance import ThreadedWebsocketManager

from bot.config import settings

logger = logging.getLogger(__name__)

TESTNET_BASE_URL = "https://testnet.binance.vision"
MAX_KLINES_LIMIT = 1000  # Binance rejects anything above this
MAX_RETRIES = 3
BACKOFF_BASE = 2.0  # seconds

# Retryable exceptions: only network and Binance API issues. Programming bugs
# (TypeError, KeyError, AttributeError, ...) propagate immediately so they
# surface in logs instead of burning ~14s on three pointless retries.
_RETRYABLE = (
    BinanceAPIException,
    BinanceRequestException,
    requests.exceptions.RequestException,
    TimeoutError,
    ConnectionError,
)


def _retry(func):
    """Decorator: retry up to MAX_RETRIES times with exponential backoff."""
    def wrapper(*args, **kwargs):
        last_exc: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except _RETRYABLE as exc:
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

        self._testnet = _testnet
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
            # Market data (klines/ticker/streams) comes from MAINNET public
            # endpoints even in testnet mode: the testnet book prints phantom
            # 1m wicks (4–9% ranges) that trigger false SL exits, and every
            # periodic testnet reset wipes kline history, starving the regime
            # detector (needs 60 bars) and bias filter (needs 22) for weeks.
            # Orders, balances and exchangeInfo stay on the testnet client.
            self._market = Client()
            logger.info(
                "BinanceClient initialised — TESTNET mode (%s), market data from mainnet",
                TESTNET_BASE_URL,
            )
        else:
            self._client = Client(_api_key, _api_secret)
            self._market = self._client
            logger.info("BinanceClient initialised — LIVE mode")

        self._twm: Optional[ThreadedWebsocketManager] = None

    @property
    def is_testnet(self) -> bool:
        return self._testnet

    @_retry
    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
        closed_only: bool = False,
    ) -> pd.DataFrame:
        """Fetch OHLCV klines.

        `closed_only=True` drops the currently-forming candle, which Binance
        always appends to the response. Every signal-generating fetch MUST set
        it: the scheduler ticks hourly while the traded timeframe is 4h, so a
        raw fetch hands the strategy a partial bar. EMA9/21 then repaint
        mid-bar and ATR is measured over an incomplete range, which understates
        it and shrinks the `1.5 × ATR` stop below what `BacktestEngine` — which
        only ever iterates closed bars — simulated. That gap, not the strategy,
        is what produced the July 2026 stop-out streak (7 of 8 closed trades).

        Leave it False for price lookups (e.g. the 1m bar used by exit checks),
        which need the in-flight price.
        """
        # +1 so dropping the forming bar still leaves `limit` closed ones.
        # Clamped because Binance rejects limit > 1000 outright.
        fetch_limit = min(limit + 1, MAX_KLINES_LIMIT) if closed_only else limit
        raw = self._market.get_klines(
            symbol=symbol, interval=interval, limit=fetch_limit
        )
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_asset_volume", "num_trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ])
        if closed_only and len(df) > 1:
            # close_time is the last millisecond of the interval; a bar is only
            # final once that instant has passed.
            now_ms = time.time() * 1000.0
            if float(df["close_time"].iloc[-1]) >= now_ms:
                df = df.iloc[:-1]
            df = df.iloc[-limit:]
        # open_time as datetime so chart consumers don't have to derive it
        # synthetically. All numeric callers index by name (open/high/low/close/
        # volume) so the extra column is non-breaking.
        ohlcv = df[["open", "high", "low", "close", "volume"]].astype(float)
        ohlcv.insert(0, "open_time", pd.to_datetime(df["open_time"], unit="ms"))
        ohlcv = ohlcv.reset_index(drop=True)
        logger.debug(
            "Fetched %d klines for %s/%s (closed_only=%s)",
            len(ohlcv), symbol, interval, closed_only,
        )
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

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = Client.ORDER_TYPE_MARKET,
    ) -> dict:
        """Place an order exactly once, even across network retries.

        Not wrapped in @_retry on purpose: a ReadTimeout can arrive AFTER
        Binance accepted the order, and a blind resend doubles the position.
        Every attempt carries the same newClientOrderId; after a network
        failure the order is looked up by that id before trying again (and a
        resend with the same id is rejected by Binance as a duplicate).
        Binance API rejections (e.g. -2010 insufficient balance) are not
        retried — they are deterministic — and stay as __cause__ so callers
        can inspect the code.
        """
        logger.info(
            "Placing order symbol=%s side=%s qty=%.5f type=%s",
            symbol, side, quantity, order_type,
        )
        client_order_id = f"tb{uuid.uuid4().hex[:30]}"
        last_exc: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                order = self._client.create_order(
                    symbol=symbol,
                    side=side,
                    type=order_type,
                    quantity=quantity,
                    newClientOrderId=client_order_id,
                )
                break
            except BinanceAPIException as exc:
                raise RuntimeError(
                    f"place_order rejected by Binance: {exc}"
                ) from exc
            except _RETRYABLE as exc:
                last_exc = exc
                existing = self._find_order(symbol, client_order_id)
                if existing is not None:
                    logger.warning(
                        "place_order: %s after submit, but the order exists "
                        "(orderId=%s status=%s) — not resending",
                        exc, existing.get("orderId"), existing.get("status"),
                    )
                    return existing
                wait = BACKOFF_BASE ** attempt
                logger.warning(
                    "Attempt %d/%d failed for place_order: %s — retrying in %.1fs",
                    attempt, MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)
        else:
            raise RuntimeError(
                f"All {MAX_RETRIES} attempts failed for place_order"
            ) from last_exc

        order_id = order.get("orderId", "N/A")
        status   = order.get("status", "N/A")
        logger.info("Order placed orderId=%s status=%s", order_id, status)
        return order

    def _find_order(self, symbol: str, client_order_id: str) -> dict | None:
        """Look an order up by client id; None if Binance has no such order.

        Any other failure is treated as "unknown" and also returns None, so the
        caller retries with the SAME client id — Binance rejects a duplicate
        id, which makes that retry safe.
        """
        try:
            return self._client.get_order(
                symbol=symbol, origClientOrderId=client_order_id,
            )
        except Exception as exc:
            logger.debug("Order lookup %s failed: %s", client_order_id, exc)
            return None

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

            # Cancel the limit order (best-effort), then re-read it: it may
            # have filled — fully or partly — between the last poll and the
            # cancel. Only the unfilled remainder may go out at MARKET,
            # otherwise the exchange position ends up larger than the DB's.
            try:
                self._client.cancel_order(symbol=symbol, orderId=order_id)
            except Exception as cancel_exc:
                logger.warning("Could not cancel LIMIT_MAKER orderId=%s: %s", order_id, cancel_exc)

            try:
                final = self._client.get_order(symbol=symbol, orderId=order_id)
            except Exception as exc:
                # Unknown fill state: sending MARKET could double the
                # position. Fail the entry instead — the caller logs it and
                # nothing is recorded; the next bar can re-enter.
                raise RuntimeError(
                    f"LIMIT_MAKER orderId={order_id} state unknown after cancel: {exc}"
                ) from exc

            executed = float(final.get("executedQty") or 0)
            remaining = quantity - executed
            if executed > 0 and remaining > 0:
                try:
                    decimals = self.get_quantity_precision(symbol)
                except Exception:
                    decimals = 8
                remaining = _round_down(remaining, decimals)
            if final.get("status") == "FILLED" or remaining <= 0:
                logger.info(
                    "LIMIT_MAKER orderId=%s filled before cancel (executed=%s)",
                    order_id, final.get("executedQty"),
                )
                return final
            if executed > 0:
                logger.info(
                    "LIMIT_MAKER orderId=%s partially filled %.8f/%.8f — MARKET for remainder %.8f",
                    order_id, executed, quantity, remaining,
                )
                market = self.place_order(symbol=symbol, side=side, quantity=remaining)
                return _merge_fills(final, market)

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
        ticker = self._market.get_symbol_ticker(symbol=symbol)
        return float(ticker["price"])

    def start_price_stream(
        self, symbol: str, on_tick: Callable[[dict], None]
    ) -> ThreadedWebsocketManager:
        """Start or reuse a single TWM to stream klines for the given symbol."""
        if self._twm is None:
            # Kline streams are public — always from mainnet (testnet streams
            # carry the same phantom-wick prices as its REST klines).
            self._twm = ThreadedWebsocketManager(
                api_key=None if self._testnet else settings.api_key,
                api_secret=None if self._testnet else settings.api_secret,
                testnet=False,
                tld="com",
            )
            self._twm.start()
            logger.info("ThreadedWebsocketManager started (market data: mainnet)")

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


def _round_down(value: float, decimals: int) -> float:
    """Round `value` DOWN to the symbol's LOT_SIZE decimals.

    The remainder of a partial fill is a difference of two LOT_SIZE-aligned
    quantities; float subtraction leaves noise (0.6000000000000001) that
    Binance would reject. Rounding down never sends more than was intended.
    """
    q = Decimal(1).scaleb(-decimals)
    return float(Decimal(repr(value)).quantize(q, rounding=ROUND_DOWN))


def _merge_fills(first: dict, second: dict) -> dict:
    """Combine a partial LIMIT_MAKER fill and its MARKET remainder into one
    result so callers' avg-fill-price maths covers the whole position."""
    merged = dict(second)
    for key in ("executedQty", "cummulativeQuoteQty"):
        total = float(first.get(key) or 0) + float(second.get(key) or 0)
        merged[key] = f"{total:.8f}"
    merged["status"] = "FILLED"
    return merged
