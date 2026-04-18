#!/usr/bin/env python3
"""Trading bot entry point."""

import argparse
import logging
import os
import signal
import sys
import time
import datetime as dt
from pathlib import Path

import schedule

from bot.adaptive.adaptor import ParameterAdaptor
from bot.config import settings
from bot.constants import ExitReason, StrategyName, TradeAction
from bot.database.db import Database
from bot.exchange.binance_client import BinanceClient
from bot.orchestrator import StrategyOrchestrator
from bot.risk.manager import RiskConfig

KLINES_LIMIT = 200
LOG_DIR = Path("logs")


def _build_client(db: Database) -> BinanceClient:
    """Return a BinanceClient configured for the current active mode."""
    from bot.credentials import decrypt
    mode = db.get_active_mode()
    if mode == "MAINNET":
        creds = db.get_mainnet_credentials()
        if creds:
            try:
                api_key    = decrypt(creds[0], settings.fernet_key)
                api_secret = decrypt(creds[1], settings.fernet_key)
                return BinanceClient(api_key=api_key, api_secret=api_secret, testnet=False)
            except Exception as exc:
                logger.error("Failed to decrypt mainnet credentials: %s — falling back to TESTNET", exc)
        else:
            logger.error("MAINNET mode set but no credentials found — falling back to TESTNET")
    return BinanceClient()


def setup_logging(level: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    fmt = "%(asctime)s  %(levelname)-8s  %(name)-30s  %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        datefmt=datefmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / "bot.log"),
        ],
    )
    # Use local time (respects TZ env var) instead of UTC in log timestamps
    logging.Formatter.converter = time.localtime


logger = logging.getLogger(__name__)

_shutdown = False


def _handle_signal(signum: int, _frame) -> None:
    global _shutdown
    logger.info("Received signal %d — shutting down gracefully...", signum)
    _shutdown = True


def _make_tick_handler(db: Database, symbol: str):
    def handle_tick(msg: dict) -> None:
        if msg.get("e") == "error":
            logger.warning("WebSocket error: %s", msg)
            return
        k = msg.get("k", {})
        if not k:
            return
        try:
            db.upsert_live_tick(
                symbol=symbol,
                price=float(k["c"]),
                open_=float(k["o"]),
                high=float(k["h"]),
                low=float(k["l"]),
                volume=float(k["v"]),
                timestamp=str(k["t"]),
            )
        except Exception as exc:
            logger.warning("Failed to upsert live tick: %s", exc)
    return handle_tick


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crypto trading bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without placing real orders",
    )
    return parser.parse_args()


def compute_drawdown(db: Database, current_balance: float) -> float:
    curve = db.get_equity_curve()
    if not curve:
        return 0.0
    peak = max(row["balance"] for row in curve)
    if peak <= 0:
        return 0.0
    return (peak - current_balance) / peak


def run_cycle(
    orchestrator: StrategyOrchestrator,
    db: Database,
    dry_run: bool,
    adaptor: ParameterAdaptor | None = None,
) -> None:
    logger.info("─── Cycle start %s ───", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    client = _build_client(db)

    try:
        df = client.get_klines(settings.symbol, settings.timeframe, KLINES_LIMIT)
    except Exception as exc:
        logger.error("Failed to fetch klines: %s", exc)
        return

    try:
        balance = client.get_balance("USDT")
    except Exception as exc:
        logger.warning("Failed to fetch balance, using last known: %s", exc)
        curve = db.get_equity_curve()
        balance = curve[-1]["balance"] if curve else settings.initial_capital

    order = orchestrator.step(df, balance)

    if order is not None:
        logger.info("Orchestrator returned order: %s", order)

        if dry_run:
            logger.info("[DRY-RUN] Would execute: %s", order)
        else:
            _execute_order(client, db, order)
    else:
        logger.info("No order this cycle")

    drawdown = compute_drawdown(db, balance)
    db.insert_equity_snapshot(balance=balance, drawdown=drawdown)
    logger.info("Equity snapshot balance=%.2f drawdown=%.4f", balance, drawdown)

    if adaptor is not None:
        curve = db.get_equity_curve()
        peak = max(row["balance"] for row in curve) if curve else balance
        adaptor.maybe_adapt(
            circuit_breaker_active=orchestrator.risk_manager.check_circuit_breaker(balance, peak)
        )

    logger.info("─── Cycle end ───")


def _execute_order(client: BinanceClient, db: Database, order: dict) -> None:
    action = order["action"]

    if action == "OPEN":
        try:
            result = client.place_order(
                symbol=settings.symbol,
                side=order["side"],
                quantity=order["quantity"],
            )
            trade_id = db.insert_trade(
                symbol=settings.symbol,
                side=order["side"],
                strategy=order["strategy"],
                regime=order["regime"],
                entry_price=order["entry_price"],
                quantity=order["quantity"],
                stop_loss=order["stop_loss"],
                take_profit=order["take_profit"],
                atr=order.get("atr"),
            )
            logger.info(
                "Opened trade id=%d orderId=%s",
                trade_id, result.get("orderId"),
            )
        except Exception as exc:
            logger.error("Failed to open position: %s", exc)

    elif action == "CLOSE":
        try:
            result = client.place_order(
                symbol=settings.symbol,
                side=order["side"],
                quantity=order["quantity"],
            )
            db.close_trade(
                trade_id=order["trade_id"],
                exit_price=order["exit_price"],
                exit_reason=order["exit_reason"],
            )
            logger.info(
                "Closed trade id=%d orderId=%s reason=%s",
                order["trade_id"], result.get("orderId"), order["exit_reason"],
            )
        except Exception as exc:
            logger.error("Failed to close position: %s", exc)


def position_manager(db: Database, dry_run: bool) -> None:
    """Check SL/TP on open position using live WebSocket price. Runs every 60s."""
    trade = db.get_open_trade()
    if trade is None:
        return

    tick = db.get_live_tick(settings.symbol)
    if tick is None:
        logger.debug("position_manager: no live tick — skipping")
        return

    price       = tick["price"]
    side        = trade["side"]
    trailing_sl = trade.get("trailing_sl")
    stop_loss   = trade["stop_loss"]
    take_profit = trade["take_profit"]
    trade_id    = trade["id"]

    reason: str | None = None

    if trailing_sl is not None:
        if (side == "BUY" and price <= trailing_sl) or \
           (side == "SELL" and price >= trailing_sl):
            reason = ExitReason.TRAILING_STOP

    if reason is None:
        if (side == "BUY" and price <= stop_loss) or \
           (side == "SELL" and price >= stop_loss):
            reason = ExitReason.STOP_LOSS
        elif (side == "BUY" and price >= take_profit) or \
             (side == "SELL" and price <= take_profit):
            reason = ExitReason.TAKE_PROFIT

    if reason is None:
        return

    logger.info(
        "position_manager: closing trade id=%d reason=%s price=%.2f",
        trade_id, reason, price,
    )

    if dry_run:
        logger.info("[DRY-RUN] position_manager would close trade id=%d", trade_id)
        return

    client     = _build_client(db)
    close_side = "SELL" if side == "BUY" else "BUY"
    _execute_order(client, db, {
        "action":      TradeAction.CLOSE,
        "side":        close_side,
        "trade_id":    trade_id,
        "quantity":    trade["quantity"],
        "exit_price":  price,
        "exit_reason": reason,
    })


def main() -> None:
    args = parse_args()
    setup_logging(settings.log_level)

    from bot.credentials import ensure_fernet_key
    fernet_key = ensure_fernet_key()
    settings.fernet_key = fernet_key

    if args.dry_run:
        logger.info("*** DRY-RUN mode — no real orders will be placed ***")
    else:
        try:
            settings.validate()
        except ValueError as exc:
            logger.error("Configuration error: %s", exc)
            sys.exit(1)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    db = Database()
    risk_config = RiskConfig(risk_per_trade=settings.risk_per_trade)
    orchestrator = StrategyOrchestrator(db=db, symbol=settings.symbol, risk_config=risk_config)
    adaptor = ParameterAdaptor(
        db=db,
        mean_reversion_strategy=orchestrator._strategies[StrategyName.MEAN_REVERSION],
        breakout_strategy=orchestrator._strategies[StrategyName.BREAKOUT],
        risk_manager=orchestrator.risk_manager,
    )

    # Build a client for the WebSocket price stream (uses startup mode)
    stream_client = _build_client(db)
    twm = stream_client.start_price_stream(
        settings.symbol,
        _make_tick_handler(db, settings.symbol),
    )

    logger.info(
        "Bot started — symbol=%s timeframe=%s dry_run=%s",
        settings.symbol, settings.timeframe, args.dry_run,
    )

    # Run immediately on startup, then schedule hourly
    run_cycle(orchestrator, db, dry_run=args.dry_run, adaptor=adaptor)

    schedule.every().hour.at(":00").do(
        run_cycle, orchestrator, db, args.dry_run, adaptor
    )
    schedule.every(60).seconds.do(position_manager, db, args.dry_run)

    while not _shutdown:
        schedule.run_pending()
        time.sleep(10)

    twm.stop()
    logger.info("WebSocket price stream stopped.")
    logger.info("Bot stopped cleanly.")


if __name__ == "__main__":
    main()
