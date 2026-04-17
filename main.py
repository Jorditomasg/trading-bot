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

from bot.config import settings
from bot.database.db import Database
from bot.exchange.binance_client import BinanceClient
from bot.orchestrator import StrategyOrchestrator
from bot.risk.manager import RiskConfig

KLINES_LIMIT = 200
LOG_DIR = Path("logs")


def setup_logging(level: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    fmt = "%(asctime)s %(levelname)-8s %(name)-30s %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
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
    client: BinanceClient,
    orchestrator: StrategyOrchestrator,
    db: Database,
    dry_run: bool,
) -> None:
    logger.info("─── Cycle start %s ───", dt.datetime.now().isoformat())

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


def main() -> None:
    args = parse_args()
    setup_logging(settings.log_level)

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
    client = BinanceClient()
    risk_config = RiskConfig(risk_per_trade=settings.risk_per_trade)
    orchestrator = StrategyOrchestrator(db=db, symbol=settings.symbol, risk_config=risk_config)

    logger.info(
        "Bot started — symbol=%s timeframe=%s testnet=%s dry_run=%s",
        settings.symbol, settings.timeframe, settings.testnet, args.dry_run,
    )

    # Run immediately on startup, then schedule hourly
    run_cycle(client, orchestrator, db, dry_run=args.dry_run)

    schedule.every().hour.at(":00").do(
        run_cycle, client, orchestrator, db, args.dry_run
    )

    while not _shutdown:
        schedule.run_pending()
        time.sleep(10)

    logger.info("Bot stopped cleanly.")


if __name__ == "__main__":
    main()
