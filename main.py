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
from bot.bias.filter import BiasFilter, BiasFilterConfig
from bot.config import settings
from bot.constants import ExitReason, StrategyName, TradeAction
from bot.database.db import Database
from bot.exchange.binance_client import BinanceClient
from bot.orchestrator import StrategyOrchestrator
from bot.risk.manager import RiskConfig
from bot.telegram_commands import TelegramCommandHandler
from bot.telegram_notifier import TelegramNotifier

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


def _apply_runtime_config(db: Database, risk_config: RiskConfig) -> None:
    """Override settings + RiskConfig from DB runtime config (written by the dashboard)."""
    cfg = db.get_runtime_config()
    if not cfg:
        return

    if "symbol" in cfg:
        settings.symbol = cfg["symbol"]
        logger.info("Runtime config: symbol=%s", cfg["symbol"])
    if "timeframe" in cfg:
        settings.timeframe = cfg["timeframe"]
        logger.info("Runtime config: timeframe=%s", cfg["timeframe"])
    if "risk_per_trade" in cfg:
        risk_config.risk_per_trade = float(cfg["risk_per_trade"])
    if "max_drawdown" in cfg:
        risk_config.max_drawdown = float(cfg["max_drawdown"])
    if "max_concurrent" in cfg:
        risk_config.max_concurrent_trades = int(cfg["max_concurrent"])
    if "trail_atr_mult" in cfg:
        risk_config.trail_atr_mult = float(cfg["trail_atr_mult"])
    if "trail_act_mult" in cfg:
        risk_config.trail_activation_mult = float(cfg["trail_act_mult"])
    if "cooldown_hours" in cfg:
        risk_config.cooldown_hours = int(cfg["cooldown_hours"])
    logger.info("Runtime config applied: %s", list(cfg.keys()))


def _apply_ema_config(db: Database, orchestrator: "StrategyOrchestrator") -> None:
    """Apply optimizer-approved EMA TP/SL multipliers to the live strategy instance."""
    from bot.constants import StrategyName
    cfg = db.get_runtime_config()
    ema_strategy = orchestrator._strategies.get(StrategyName.EMA_CROSSOVER)
    if ema_strategy is None:
        return
    if "ema_stop_mult" in cfg:
        ema_strategy.config.stop_atr_mult = float(cfg["ema_stop_mult"])
        logger.info("Runtime config: ema_stop_mult=%.2f", float(cfg["ema_stop_mult"]))
    if "ema_tp_mult" in cfg:
        ema_strategy.config.tp_atr_mult = float(cfg["ema_tp_mult"])
        logger.info("Runtime config: ema_tp_mult=%.2f", float(cfg["ema_tp_mult"]))


def _init_quantity_precision(orchestrator: StrategyOrchestrator, db: Database) -> None:
    """Fetch the real LOT_SIZE stepSize for the configured symbol and update risk config."""
    try:
        client    = _build_client(db)
        precision = client.get_quantity_precision(settings.symbol)
        orchestrator.risk_manager.config.quantity_precision = precision
    except Exception as exc:
        logger.warning(
            "Could not fetch quantity precision for %s: %s — using default %d",
            settings.symbol, exc,
            orchestrator.risk_manager.config.quantity_precision,
        )


# Price precision for limit entry orders (decimal places for price, e.g. 2 for BTC)
_price_precision: int = 2


def _init_price_precision(db: Database) -> None:
    """Fetch PRICE_FILTER tickSize for the configured symbol and cache globally."""
    global _price_precision
    try:
        client = _build_client(db)
        _price_precision = client.get_price_precision(settings.symbol)
        logger.info("Price precision for %s: %d", settings.symbol, _price_precision)
    except Exception as exc:
        logger.warning(
            "Could not fetch price precision for %s: %s — using default %d",
            settings.symbol, exc, _price_precision,
        )


def compute_drawdown(db: Database, current_balance: float) -> float:
    peak = db.get_peak_capital() or current_balance
    if peak <= 0:
        return 0.0
    return (peak - current_balance) / peak


def run_cycle(
    orchestrator: StrategyOrchestrator,
    db: Database,
    dry_run: bool,
    adaptor: ParameterAdaptor | None = None,
    notifier: TelegramNotifier | None = None,
) -> None:
    logger.info("─── Cycle start %s ───", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    if db.get_bot_paused():
        logger.info("Bot is paused — skipping cycle")
        return

    client = _build_client(db)

    try:
        df = client.get_klines(settings.symbol, settings.timeframe, KLINES_LIMIT)
    except Exception as exc:
        logger.error("Failed to fetch klines: %s", exc)
        return

    # Daily klines for BiasFilter — backtest-proven: daily EMA9/21 gate
    # outperforms 4h EMA gate (PF 1.19-1.30 vs 0.82-0.93 with taker fees)
    df_4h = None
    try:
        df_4h = client.get_klines(settings.symbol, "1d", 60)
    except Exception as exc:
        logger.warning(
            "Failed to fetch daily klines: %s — bias filter will block signals this cycle", exc
        )

    try:
        balance = client.get_balance("USDT")
    except Exception as exc:
        logger.warning("Failed to fetch balance, using last known: %s", exc)
        curve = db.get_equity_curve()
        balance = curve[-1]["balance"] if curve else settings.initial_capital

    # Snapshot circuit breaker state before step to detect new triggers
    breaker_was_active = orchestrator.risk_manager._breaker_triggered_at is not None

    orders = orchestrator.step(df, balance, df_4h)

    # Notify if circuit breaker just fired this cycle
    if notifier and not breaker_was_active:
        if orchestrator.risk_manager._breaker_triggered_at is not None:
            drawdown = compute_drawdown(db, balance)
            notifier.circuit_breaker(drawdown, db.get_active_mode())

    if orders:
        for order in orders:
            logger.info("Orchestrator returned order: %s", order)
            if dry_run:
                logger.info("[DRY-RUN] Would execute: %s", order)
            else:
                _execute_order(client, db, order, notifier)
    else:
        logger.info("No orders this cycle")

    drawdown = compute_drawdown(db, balance)
    db.insert_equity_snapshot(balance=balance, drawdown=drawdown)
    logger.info("Equity snapshot balance=%.2f drawdown=%.4f", balance, drawdown)

    if adaptor is not None:
        peak = db.get_peak_capital() or balance
        adaptor.maybe_adapt(
            circuit_breaker_active=orchestrator.risk_manager.check_circuit_breaker(balance, peak)
        )

    logger.info("─── Cycle end ───")


def _avg_fill_price(order_result: dict) -> float | None:
    """Extract the average fill price from a Binance order result.

    Uses cummulativeQuoteQty / executedQty (weighted average across fills).
    Returns None if the fields are missing or zero.
    """
    try:
        executed = float(order_result.get("executedQty", 0))
        quote    = float(order_result.get("cummulativeQuoteQty", 0))
        if executed > 0 and quote > 0:
            return quote / executed
    except (TypeError, ValueError):
        pass
    return None


def _execute_order(
    client: BinanceClient,
    db: Database,
    order: dict,
    notifier: TelegramNotifier | None = None,
) -> None:
    action = order["action"]
    mode   = db.get_active_mode()

    if action == "OPEN":
        try:
            result = client.place_entry_order(
                symbol=settings.symbol,
                side=order["side"],
                quantity=order["quantity"],
                entry_price=order["entry_price"],
                price_precision=_price_precision,
            )
            # Use the actual fill price from the exchange; fall back to signal price
            actual_entry = _avg_fill_price(result) or order["entry_price"]
            trade_id = db.insert_trade(
                symbol=settings.symbol,
                side=order["side"],
                strategy=order["strategy"],
                regime=order["regime"],
                entry_price=actual_entry,
                quantity=order["quantity"],
                stop_loss=order["stop_loss"],
                take_profit=order["take_profit"],
                atr=order.get("atr"),
                timeframe=order.get("timeframe", "1h"),
            )
            logger.info(
                "Opened trade id=%d orderId=%s",
                trade_id, result.get("orderId"),
            )
            if notifier:
                notifier.trade_opened(order, mode)
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
            if notifier:
                trade = db.get_trade(order["trade_id"])
                pnl   = trade["pnl"] if trade else 0.0
                notifier.trade_closed(trade or order, pnl, order["exit_reason"], mode)
        except Exception as exc:
            logger.error("Failed to close position: %s", exc)


def _manage_single_position(
    trade: dict,
    price: float,
    db: Database,
    dry_run: bool,
    risk_config: "RiskConfig | None",
    notifier: "TelegramNotifier | None",
) -> None:
    """Manage trailing stop ratchet and SL/TP exit for one open trade."""
    trade_id    = trade["id"]
    side        = trade["side"]
    entry_price = trade["entry_price"]
    stop_loss   = trade["stop_loss"]
    take_profit = trade["take_profit"]
    trade_atr   = trade.get("atr")
    trailing_sl = trade.get("trailing_sl")

    # Guard: re-verify trade is still open (race condition with run_cycle)
    fresh = db.get_trade(trade_id)
    if fresh is None or fresh.get("exit_price") is not None:
        logger.debug("position_manager: trade id=%d already closed — skipping", trade_id)
        return

    # ── Ratchet trailing stop ─────────────────────────────────────────────────
    if trade_atr and risk_config is not None:
        trail_dist = risk_config.trail_atr_mult * trade_atr
        activation = risk_config.trail_activation_mult * trade_atr

        if side == "BUY" and price >= entry_price + activation:
            new_trail = price - trail_dist
            if trailing_sl is None or new_trail > trailing_sl:
                db.update_trailing_sl(trade_id, new_trail)
                trailing_sl = new_trail
                logger.debug(
                    "position_manager: trailing SL ratcheted to %.2f (price=%.2f)",
                    new_trail, price,
                )
        elif side == "SELL" and price <= entry_price - activation:
            new_trail = price + trail_dist
            if trailing_sl is None or new_trail < trailing_sl:
                db.update_trailing_sl(trade_id, new_trail)
                trailing_sl = new_trail
                logger.debug(
                    "position_manager: trailing SL ratcheted to %.2f (price=%.2f)",
                    new_trail, price,
                )

    # ── Exit condition check ──────────────────────────────────────────────────
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

    try:
        client     = _build_client(db)
        close_side = "SELL" if side == "BUY" else "BUY"
        _execute_order(client, db, {
            "action":      TradeAction.CLOSE,
            "side":        close_side,
            "trade_id":    trade_id,
            "quantity":    trade["quantity"],
            "exit_price":  price,
            "exit_reason": reason,
        }, notifier)
    except Exception as exc:
        logger.error(
            "position_manager: failed to close trade id=%d: %s", trade_id, exc
        )


def position_manager(
    db: Database,
    dry_run: bool,
    risk_config: "RiskConfig | None" = None,
    notifier: "TelegramNotifier | None" = None,
) -> None:
    """Check SL/TP and ratchet trailing stop for all open trades. Runs every 60s."""
    trades = db.get_open_trades()
    if not trades:
        return

    tick = db.get_live_tick(settings.symbol)
    if tick is None:
        logger.debug("position_manager: no live tick — skipping")
        return

    price = tick["price"]
    for trade in trades:
        _manage_single_position(trade, price, db, dry_run, risk_config, notifier)


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
    _apply_runtime_config(db, risk_config)
    bias_filter = BiasFilter(BiasFilterConfig())
    orchestrator = StrategyOrchestrator(
        db=db,
        symbol=settings.symbol,
        risk_config=risk_config,
        bias_filter=bias_filter,
        timeframe=settings.timeframe,
    )
    adaptor = ParameterAdaptor(
        db=db,
        mean_reversion_strategy=orchestrator._strategies[StrategyName.MEAN_REVERSION],
        breakout_strategy=orchestrator._strategies[StrategyName.BREAKOUT],
        risk_manager=orchestrator.risk_manager,
    )
    _apply_ema_config(db, orchestrator)
    _init_quantity_precision(orchestrator, db)
    _init_price_precision(db)

    # Telegram — always instantiated; no-ops when unconfigured
    notifier    = TelegramNotifier(db=db)
    cmd_handler = TelegramCommandHandler(db=db, notifier=notifier)
    cmd_handler.start()

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
    notifier.bot_started(args.dry_run, db.get_active_mode())
    notifier.register_commands()

    # Run immediately on startup, then schedule hourly
    run_cycle(orchestrator, db, dry_run=args.dry_run, adaptor=adaptor, notifier=notifier)

    schedule.every().hour.at(":00").do(
        run_cycle, orchestrator, db, args.dry_run, adaptor, notifier
    )
    schedule.every(60).seconds.do(
        position_manager, db, args.dry_run, orchestrator.risk_manager.config, notifier
    )

    while not _shutdown:
        if db.consume_restart_request():
            logger.info("Restart requested via dashboard — exiting for container restart.")
            break
        schedule.run_pending()
        time.sleep(10)

    notifier.bot_stopped()
    cmd_handler.stop()
    twm.stop()
    logger.info("WebSocket price stream stopped.")
    logger.info("Bot stopped cleanly.")


if __name__ == "__main__":
    main()
