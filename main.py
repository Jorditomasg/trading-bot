#!/usr/bin/env python3
"""Trading bot entry point."""

import argparse
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
import datetime as dt

import pandas as pd
import schedule

from bot.adaptive.adaptor import ParameterAdaptor
from bot.bias.filter import BiasFilter, BiasFilterConfig
from bot.config import settings
from bot.constants import ExitReason, StrategyName, TradeAction
from bot.database.db import Database
from bot.exchange.binance_client import BinanceClient
from bot.logging_setup import setup_logging
from bot.optimizer.auto_optimizer import run_and_apply, should_run
from bot.optimizer.auto_entry_quality_optimizer import (
    run_and_apply as eq_run_and_apply,
    should_run as eq_should_run,
)
from bot.orchestrator import StrategyOrchestrator
from bot.risk.manager import RiskConfig
from bot.telegram_commands import TelegramCommandHandler
from bot.telegram_notifier import TelegramNotifier

KLINES_LIMIT = 200


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
        settings.risk_per_trade = float(cfg["risk_per_trade"])  # keep settings in sync
    if "max_drawdown" in cfg:
        risk_config.max_drawdown = float(cfg["max_drawdown"])
    if "cooldown_hours" in cfg:
        risk_config.cooldown_hours = int(cfg["cooldown_hours"])
    logger.info("Runtime config applied: %s", list(cfg.keys()))


def _seed_optimized_defaults(db: Database) -> None:
    """Write validated optimal parameters to the DB on first run.

    Only sets values that are not already present — never overwrites user customisations.
    These replace the old .env-based defaults (SYMBOL, TIMEFRAME, RISK_PER_TRADE).
    Values come from the 3-year backtest (Apr 2022–Apr 2025, BTCUSDT 4h, long-only):
      Ann=22.5%  PF=1.551  Sharpe=9.63  DD=20.5%  (at 2% risk)
    """
    cfg = db.get_runtime_config()

    # ema_tp_mult bumped from 4.5 → 5.0 after the May 2026 optimizer bake-off
    # (data/audits/B_vs_C1_apples_to_apples.json): SL=1.5,TP=5.0 wins 10/10
    # walk-forward windows over C1's 4.5 (mean PF 1.38 vs 1.27, Calmar 13.5 vs
    # 8.7, paired t p<0.001). Audit: docs/audits/A_walk_forward_2026-05-14.md.
    #
    # Both auto-optimizers default OFF: the May 2026 audit proved the existing
    # methodology (single 180d window, sort by PF) overfits recent noise.
    # Sub-project D will deliver a walk-forward + Calmar methodology before
    # they can be safely re-enabled.
    defaults = {
        "symbol":          "BTCUSDT",
        "timeframe":       "4h",
        "risk_per_trade":  "0.015",   # 1.5% = Quarter-Kelly; safe, ~17% annual
        "ema_stop_mult":   "1.5",
        "ema_tp_mult":     "5.0",     # B-pick (audit-validated, May 2026)
        "ema_max_dist_atr":"1.0",
        "long_only":       "true",
        "backtest_cost_per_side":     "0.001",   # 0.10% per side (Binance VIP-0 spot, no BNB discount)
        "auto_optimizer_enabled":     "false",   # audited NO-GO methodology — see CLAUDE.md gotcha #33
        "auto_entry_quality_enabled": "false",   # sibling optimizer, unaudited
    }

    to_seed = {k: v for k, v in defaults.items() if k not in cfg}
    if to_seed:
        db.set_runtime_config(**to_seed)
        logger.info("Seeded optimized defaults into DB: %s", list(to_seed.keys()))


def _build_bias_filter(db: Database) -> BiasFilter:
    """Construct BiasFilter using parameters persisted by the dashboard."""
    cfg = db.get_runtime_config()
    return BiasFilter(BiasFilterConfig(
        neutral_passthrough=cfg.get("bias_neutral_passthrough", "true") == "true",
        neutral_threshold_pct=float(cfg.get("bias_neutral_threshold", "0.001")),
        block_on_data_failure=cfg.get("bias_block_on_data_failure", "false") == "true",
    ))


def _apply_ema_config(db: Database, orchestrator: "StrategyOrchestrator") -> None:
    """Apply optimizer-approved EMA TP/SL multipliers and dashboard flags to the live strategy."""
    from bot.constants import StrategyName
    cfg = db.get_runtime_config()
    try:
        ema_strategy = orchestrator.get_strategy(StrategyName.EMA_CROSSOVER)
    except KeyError:
        return
    if "ema_stop_mult" in cfg:
        ema_strategy.config.stop_atr_mult = float(cfg["ema_stop_mult"])
        logger.info("Runtime config: ema_stop_mult=%.2f", float(cfg["ema_stop_mult"]))
    if "ema_tp_mult" in cfg:
        ema_strategy.config.tp_atr_mult = float(cfg["ema_tp_mult"])
        logger.info("Runtime config: ema_tp_mult=%.2f", float(cfg["ema_tp_mult"]))
    if "long_only" in cfg:
        long_only = cfg["long_only"] == "true"
        ema_strategy.config.long_only = long_only
        logger.info("Runtime config: long_only=%s", long_only)
    if "ema_vol_mult" in cfg:
        ema_strategy.config.volume_multiplier = float(cfg["ema_vol_mult"])
        logger.info("Runtime config: ema_vol_mult=%.2f", float(cfg["ema_vol_mult"]))
    if "ema_bar_dir" in cfg:
        val = cfg["ema_bar_dir"] == "true"
        ema_strategy.config.require_bar_direction = val
        logger.info("Runtime config: ema_bar_dir=%s", val)
    if "ema_momentum" in cfg:
        val = cfg["ema_momentum"] == "true"
        ema_strategy.config.require_ema_momentum = val
        logger.info("Runtime config: ema_momentum=%s", val)
    if "ema_min_atr" in cfg:
        ema_strategy.config.min_atr_pct = float(cfg["ema_min_atr"])
        logger.info("Runtime config: ema_min_atr=%.4f", float(cfg["ema_min_atr"]))
    if "ema_max_dist_atr" in cfg:
        ema_strategy.config.max_distance_atr = float(cfg["ema_max_dist_atr"])
        logger.info("Runtime config: ema_max_dist_atr=%.3f", float(cfg["ema_max_dist_atr"]))


def _init_quantity_precision(orchestrator: StrategyOrchestrator, db: Database) -> None:
    """Fetch the real LOT_SIZE stepSize for the configured symbol and update risk config."""
    try:
        client    = _build_client(db)
        precision = client.get_quantity_precision(orchestrator.symbol)
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


def compute_drawdown(db: Database) -> float:
    """Compute drawdown from TRADING EQUITY (baseline + realized PnL), not raw balance.

    Uses the same trading_equity formula as orchestrator.step() so the displayed
    drawdown matches the circuit breaker's input (Risk A from design Decision 4).

    Returns 0.0 when no peak has been established yet (safe default).
    See gotcha #31 for the semantic shift (May 2026).
    """
    baseline = db.get_account_baseline() or 0.0
    closed_pnl = db.get_closed_pnl_sum()
    trading_equity = baseline + closed_pnl
    peak = db.get_peak_capital() or trading_equity
    if peak <= 0:
        return 0.0
    return max(0.0, (peak - trading_equity) / peak)


def _init_account_baseline(db: Database, client: BinanceClient) -> None:
    """One-shot lazy seed of account_baseline. Called once at startup, idempotent.

    Algorithm: account_baseline = current_USDT_balance - SUM(pnl_all_closed_trades).
    Negative result → falls back to current_balance (logs WARNING). On balance-fetch
    failure → skips seeding; retried on next bot restart.

    Also resets peak_capital so the new HWM ratchets from the new semantic (trading
    equity, not raw balance). See design Decision 6 and gotcha #31.
    """
    if db.get_account_baseline() is not None:
        return  # already seeded — idempotent no-op

    try:
        current_balance = client.get_balance("USDT")
    except Exception as exc:
        logger.error(
            "Could not seed account_baseline: balance fetch failed (%s). "
            "Will retry on next bot restart.",
            exc,
        )
        return  # do NOT seed with a wrong value

    historical_pnl = db.get_closed_pnl_sum()  # cross-symbol
    baseline = current_balance - historical_pnl

    if baseline < 0:
        logger.warning(
            "Back-computed baseline=%.2f is negative (current=%.2f − pnl=%.2f). "
            "External deposits or historical data mismatch likely. "
            "Falling back to current_balance as account_baseline.",
            baseline, current_balance, historical_pnl,
        )
        baseline = current_balance

    db.set_account_baseline(baseline)
    # Reset peak_capital so it ratchets from trading equity on the first live cycle.
    db.reset_peak_capital(value=None, clear_breaker=True)

    logger.info(
        "Seeded account_baseline=%.2f (current=%.2f, historical_pnl=%.2f). "
        "peak_capital reset to current trading equity; use /reset_hwm if this looks wrong.",
        baseline, current_balance, historical_pnl,
    )


def run_cycle(
    orchestrator: StrategyOrchestrator,
    db: Database,
    dry_run: bool,
    adaptor: ParameterAdaptor | None = None,
    notifier: TelegramNotifier | None = None,
    n_symbols: int = 1,
) -> None:
    sym = orchestrator.symbol
    logger.info(
        "─── [%s] Cycle start %s ───",
        sym, dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    if db.get_bot_paused():
        logger.info("[%s] Bot is paused — skipping cycle", sym)
        return

    client = _build_client(db)

    try:
        df = client.get_klines(sym, settings.timeframe, KLINES_LIMIT)
    except Exception as exc:
        logger.error("[%s] Failed to fetch klines: %s", sym, exc)
        return

    # Daily klines for BiasFilter — backtest-proven: daily EMA9/21 gate
    # outperforms 4h EMA gate (PF 1.19-1.30 vs 0.82-0.93 with taker fees)
    df_4h = None
    try:
        df_4h = client.get_klines(sym, "1d", 60)
    except Exception as exc:
        logger.warning(
            "[%s] Failed to fetch daily klines: %s — BiasFilter will use NEUTRAL "
            "(signals pass if neutral_passthrough=True, blocked if block_on_data_failure=True)",
            sym, exc,
        )

    # Weekly klines for momentum filter
    df_weekly: pd.DataFrame | None = None
    try:
        df_weekly = client.get_klines(sym, "1w", 60)
    except Exception as exc:
        logger.warning(
            "[%s] Failed to fetch weekly klines: %s — momentum filter will use BULLISH (fail-open)",
            sym, exc,
        )

    try:
        total_balance = client.get_balance("USDT")
    except Exception as exc:
        logger.warning("[%s] Failed to fetch balance, using last known: %s", sym, exc)
        curve = db.get_equity_curve()
        total_balance = curve[-1]["balance"] if curve else settings.initial_capital

    # Allocate capital evenly across active symbols so one symbol can't drain the pool
    balance = total_balance / max(1, n_symbols)
    if n_symbols > 1:
        logger.info(
            "[%s] Capital allocation: total=%.2f / %d symbols → allocated=%.2f",
            sym, total_balance, n_symbols, balance,
        )

    # Snapshot circuit breaker state before step to detect new triggers
    breaker_was_active = orchestrator.risk_manager._breaker_triggered_at is not None

    orders = orchestrator.step(df, balance, df_high=df_4h, df_weekly=df_weekly, total_balance=total_balance)

    # Notify if circuit breaker just fired this cycle
    if notifier and not breaker_was_active:
        if orchestrator.risk_manager._breaker_triggered_at is not None:
            drawdown = compute_drawdown(db)
            notifier.circuit_breaker(drawdown, db.get_active_mode())

    if orders:
        for order in orders:
            logger.info("[%s] Orchestrator returned order: %s", sym, order)
            if dry_run:
                logger.info("[%s] [DRY-RUN] Would execute: %s", sym, order)
            else:
                _execute_order(client, db, order, notifier)
    else:
        logger.info("[%s] No orders this cycle", sym)

    drawdown = compute_drawdown(db)
    db.insert_equity_snapshot(balance=total_balance, drawdown=drawdown)
    logger.info("[%s] Equity snapshot total_balance=%.2f drawdown=%.4f", sym, total_balance, drawdown)

    if adaptor is not None:
        peak = db.get_peak_capital() or total_balance
        adaptor.maybe_adapt(
            circuit_breaker_active=orchestrator.risk_manager.check_circuit_breaker(total_balance, peak)
        )

    logger.info("─── [%s] Cycle end ───", sym)


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


_DB_RETRY_ATTEMPTS = 3
_DB_RETRY_BACKOFF  = 0.5  # seconds; doubles each attempt

# Only retry SQLite errors that can plausibly be transient (locked DB, brief
# I/O contention, busy timeouts). Programming bugs (TypeError, KeyError,
# AttributeError, ValueError on bad args) propagate immediately so they
# surface in logs instead of burning ~3.5s on three pointless retries.
_DB_RETRYABLE = (sqlite3.OperationalError, sqlite3.DatabaseError)


def _retry_db_write(op_name: str, fn, *args, **kwargs):
    """Retry a DB write with exponential backoff. Raises on final failure.

    Used to harden the post-exchange DB writes in `_execute_order`. The
    Binance order has already been placed when this is called — retrying
    transient SQLite errors protects against orphaned positions where the
    exchange filled but the DB didn't record.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _DB_RETRY_ATTEMPTS + 1):
        try:
            return fn(*args, **kwargs)
        except _DB_RETRYABLE as exc:
            last_exc = exc
            wait = _DB_RETRY_BACKOFF * (2 ** (attempt - 1))
            logger.warning(
                "DB write '%s' attempt %d/%d failed: %s — retrying in %.1fs",
                op_name, attempt, _DB_RETRY_ATTEMPTS, exc, wait,
            )
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


def _alert_orphan_position(
    notifier: TelegramNotifier | None,
    op: str,
    symbol: str,
    order: dict,
    exchange_result: dict,
    exc: Exception,
) -> None:
    """Last-resort alert when an exchange order succeeds but DB write fails.

    The position physically exists (or was closed) on Binance, but the DB no
    longer reflects reality. The bot will misbehave on the next cycle until a
    human reconciles. Surfacing the discrepancy via logs + Telegram is the
    minimum viable safety net — auto-undoing the order risks compounding the
    problem (price moved, balance, fees), so we do not attempt it.
    """
    order_id = exchange_result.get("orderId", "N/A") if isinstance(exchange_result, dict) else "N/A"
    logger.critical(
        "[%s] ORPHANED %s — exchange filled (orderId=%s) but DB write failed after %d retries: %s. "
        "Trade details: %s. Manual reconciliation required.",
        symbol, op, order_id, _DB_RETRY_ATTEMPTS, exc, order,
    )
    if notifier is not None:
        notifier.alert(
            f"<b>ORPHANED {op}</b> on <code>{symbol}</code>\n"
            f"Exchange orderId: <code>{order_id}</code>\n"
            f"Side: <code>{order.get('side', '?')}</code>  "
            f"Qty: <code>{order.get('quantity', 0):.5f}</code>\n"
            f"DB write failed: <code>{exc}</code>\n"
            f"Manual reconciliation required."
        )


def _execute_order(
    client: BinanceClient,
    db: Database,
    order: dict,
    notifier: TelegramNotifier | None = None,
) -> None:
    action = order["action"]
    symbol = order["symbol"]
    mode   = db.get_active_mode()

    if action == "OPEN":
        try:
            result = client.place_entry_order(
                symbol=symbol,
                side=order["side"],
                quantity=order["quantity"],
                entry_price=order["entry_price"],
                price_precision=_price_precision,
            )
        except Exception as exc:
            logger.error("[%s] Failed to place open order: %s", symbol, exc)
            return

        # Order is filled. From here on, any exception risks an orphan — the
        # position exists on the exchange but not in the DB. Retry DB writes
        # before giving up; on final failure raise an explicit orphan alert.
        actual_entry = _avg_fill_price(result) or order["entry_price"]
        try:
            trade_id = _retry_db_write(
                "insert_trade", db.insert_trade,
                symbol=symbol,
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
        except Exception as exc:
            _alert_orphan_position(notifier, "OPEN", symbol, order, result, exc)
            return

        logger.info(
            "[%s] Opened trade id=%d orderId=%s",
            symbol, trade_id, result.get("orderId"),
        )
        if notifier:
            notifier.trade_opened({**order, "symbol": symbol}, mode)

    elif action == "CLOSE":
        try:
            result = client.place_order(
                symbol=symbol,
                side=order["side"],
                quantity=order["quantity"],
            )
        except Exception as exc:
            logger.error("[%s] Failed to place close order: %s", symbol, exc)
            return

        # Order is filled on the exchange. Retry the DB close before alerting.
        try:
            _retry_db_write(
                "close_trade", db.close_trade,
                trade_id=order["trade_id"],
                exit_price=order["exit_price"],
                exit_reason=order["exit_reason"],
            )
        except Exception as exc:
            _alert_orphan_position(notifier, "CLOSE", symbol, order, result, exc)
            return

        logger.info(
            "[%s] Closed trade id=%d orderId=%s reason=%s",
            symbol, order["trade_id"], result.get("orderId"), order["exit_reason"],
        )
        if notifier:
            trade = db.get_trade(order["trade_id"])
            pnl   = trade["pnl"] if trade else 0.0
            notifier.trade_closed(trade or order, pnl, order["exit_reason"], mode)


def _check_intra_bar_exit(
    client: BinanceClient,
    trade: dict,
) -> tuple[str, float] | None:
    """Detect SL/TP hit using the latest 1m bars' high/low.

    The live_tick price (last WS event) can miss intra-second wicks because
    it samples discrete trade events. Binance's 1m kline aggregates EVERY
    trade in the minute, so its high/low always captures the wick.

    Exit price is the bar's CLOSE, not the SL/TP level — honest about
    real-world fill: when we send a market order after detecting the wick,
    Binance fills at spot, not at the level.

    Empirically validated on 3yr BTC 4h: this approach beats both the
    current live behaviour (close-only) by +1.25pp annual return and the
    idealised 'close at level' baseline by 24% on max drawdown.
    See scripts/test_wick_variants.py.
    """
    try:
        df = client.get_klines(trade["symbol"], "1m", limit=2)
    except Exception as exc:
        logger.debug("[%s] intra-bar wick check failed: %s", trade["symbol"], exc)
        return None
    if df.empty:
        return None

    sl    = trade["stop_loss"]
    tp    = trade["take_profit"]
    side  = trade["side"]
    entry = trade["entry_price"]

    # Iterate oldest → newest so an SL/TP on the older bar takes precedence.
    # When both SL and TP are wicked in the same bar, intra-bar order is
    # unobservable — but the live bot reacts AFTER the wick and fills at the
    # bar close (see docstring). Pick the reason that matches close direction:
    # if close is on the winning side of entry, outcome is TP; else SL. The
    # backtest engine keeps the "SL wins" conservative rule, which is the
    # statistically-correct fallback when there's no real fill price to anchor.
    for _, bar in df.iterrows():
        high  = float(bar["high"])
        low   = float(bar["low"])
        close = float(bar["close"])

        if side == "BUY":
            sl_hit = low  <= sl
            tp_hit = high >= tp
            if sl_hit and tp_hit:
                reason = ExitReason.TAKE_PROFIT if close > entry else ExitReason.STOP_LOSS
                return (reason, close)
            if sl_hit:
                return (ExitReason.STOP_LOSS, close)
            if tp_hit:
                return (ExitReason.TAKE_PROFIT, close)
        else:  # SELL
            sl_hit = high >= sl
            tp_hit = low  <= tp
            if sl_hit and tp_hit:
                reason = ExitReason.TAKE_PROFIT if close < entry else ExitReason.STOP_LOSS
                return (reason, close)
            if sl_hit:
                return (ExitReason.STOP_LOSS, close)
            if tp_hit:
                return (ExitReason.TAKE_PROFIT, close)

    return None


def _manage_single_position(
    trade: dict,
    db: Database,
    client: BinanceClient,
    dry_run: bool,
    risk_config: "RiskConfig | None",
    notifier: "TelegramNotifier | None",
) -> None:
    """Check SL/TP exit for one open trade.

    Exit detection runs in two stages:
    1. Intra-bar wick check via 1m kline high/low — primary path, catches
       mechas that live_tick can miss.
    2. live_tick spot price — fallback when kline fetch fails or returns
       no data.
    """
    trade_id    = trade["id"]
    sym         = trade["symbol"]
    side        = trade["side"]
    stop_loss   = trade["stop_loss"]
    take_profit = trade["take_profit"]

    # Guard: re-verify trade is still open (race condition with run_cycle)
    fresh = db.get_trade(trade_id)
    if fresh is None or fresh.get("exit_price") is not None:
        logger.debug("[%s] position_manager: trade id=%d already closed — skipping", sym, trade_id)
        return

    reason: str | None = None
    exit_price: float | None = None

    # 1. Primary: intra-bar wick detection
    intra = _check_intra_bar_exit(client, trade)
    if intra is not None:
        reason, exit_price = intra
        logger.info(
            "[%s] intra-bar wick detected: reason=%s exit_price=%.2f (1m bar close)",
            sym, reason, exit_price,
        )
    else:
        # 2. Fallback: live_tick spot
        tick = db.get_live_tick(sym)
        if tick is None:
            logger.debug("[%s] position_manager: no live tick — skipping", sym)
            return
        price = tick["price"]
        if (side == "BUY" and price <= stop_loss) or \
           (side == "SELL" and price >= stop_loss):
            reason, exit_price = ExitReason.STOP_LOSS, price
        elif (side == "BUY" and price >= take_profit) or \
             (side == "SELL" and price <= take_profit):
            reason, exit_price = ExitReason.TAKE_PROFIT, price

    if reason is None:
        return

    logger.info(
        "[%s] position_manager: closing trade id=%d reason=%s exit_price=%.2f",
        sym, trade_id, reason, exit_price,
    )

    if dry_run:
        logger.info("[%s] [DRY-RUN] position_manager would close trade id=%d", sym, trade_id)
        return

    try:
        close_side = "SELL" if side == "BUY" else "BUY"
        _execute_order(client, db, {
            "action":      TradeAction.CLOSE,
            "symbol":      sym,
            "side":        close_side,
            "trade_id":    trade_id,
            "quantity":    trade["quantity"],
            "exit_price":  exit_price,
            "exit_reason": reason,
        }, notifier)
    except Exception as exc:
        logger.error(
            "[%s] position_manager: failed to close trade id=%d: %s", sym, trade_id, exc
        )


def position_manager(
    db: Database,
    dry_run: bool,
    risk_config: "RiskConfig | None" = None,
    notifier: "TelegramNotifier | None" = None,
) -> None:
    """Check SL/TP for all open trades. Runs every 60s.

    Detection uses the bar's high/low (intra-bar wick) — see _check_intra_bar_exit
    for empirical justification. Live tick is the fallback.
    """
    trades = db.get_open_trades()
    if not trades:
        return

    client = _build_client(db)
    for trade in trades:
        _manage_single_position(trade, db, client, dry_run, risk_config, notifier)


def _launch_auto_optimizer(
    db: Database,
    orchestrator: StrategyOrchestrator,
    notifier: TelegramNotifier | None,
) -> None:
    """Run the auto-optimizer in a daemon thread so it never blocks the main loop.

    On success: hot-patches the running EMA strategy (no restart needed) and
    sends a Telegram notification with the new parameters.
    """
    def _worker() -> None:
        if _shutdown:
            logger.info("Auto-optimizer: shutdown in progress — skipping run")
            return

        def _on_applied(old_params: dict, new_params: dict) -> None:
            # Hot-reload: patch the live strategy object without restart
            _apply_ema_config(db, orchestrator)
            logger.info("Auto-optimizer: hot-reloaded EMA config into running strategy")
            if notifier:
                notifier.optimizer_applied(old_params, new_params, db.get_active_mode())

        try:
            run_and_apply(
                db=db,
                symbol=settings.symbol,
                timeframe=settings.timeframe,
                risk_per_trade=settings.risk_per_trade,
                on_applied=_on_applied,
            )
        except RuntimeError as exc:
            # Interpreter shutting down — daemon thread can't finish writes
            logger.info("Auto-optimizer: aborted during shutdown (%s)", exc)
        except Exception as exc:
            logger.error("Auto-optimizer: unhandled error: %s", exc, exc_info=True)

    t = threading.Thread(target=_worker, name="auto-optimizer", daemon=True)
    t.start()
    logger.info("Auto-optimizer: background thread started")


def _launch_auto_entry_quality_optimizer(
    db: Database,
    orchestrator: StrategyOrchestrator,
    notifier: TelegramNotifier | None,
) -> None:
    """Run the entry-quality optimizer in a daemon thread."""
    def _worker() -> None:
        if _shutdown:
            logger.info("Auto entry-quality optimizer: shutdown in progress — skipping run")
            return

        def _on_applied(old_params: dict, new_params: dict) -> None:
            _apply_ema_config(db, orchestrator)
            logger.info("Auto entry-quality optimizer: hot-reloaded EMA config into running strategy")

        try:
            eq_run_and_apply(
                db=db,
                symbol=settings.symbol,
                timeframe=settings.timeframe,
                risk_per_trade=settings.risk_per_trade,
                on_applied=_on_applied,
            )
        except RuntimeError as exc:
            logger.info("Auto entry-quality optimizer: aborted during shutdown (%s)", exc)
        except Exception as exc:
            logger.error("Auto entry-quality optimizer: unhandled error: %s", exc, exc_info=True)

    t = threading.Thread(target=_worker, name="auto-eq-optimizer", daemon=True)
    t.start()
    logger.info("Auto entry-quality optimizer: background thread started")


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

    # Seed optimised trading defaults on first run (no-op if already in DB)
    _seed_optimized_defaults(db)

    # Seed Telegram config from .env on first run (no-op if already set in DB)
    if settings.telegram_token and not db.has_telegram_config():
        db.save_telegram_config(
            settings.telegram_token,
            settings.telegram_chat_id,
            settings.telegram_enabled,
        )
        logger.info("Telegram config seeded from .env")

    # Apply DB runtime config to settings early so symbol/timeframe/risk are DB-driven
    _apply_runtime_config(db, RiskConfig())  # throws away the RiskConfig — we only want settings mutation

    # Symbol list: from bot_config if set, else fall back to .env SYMBOL
    symbols = db.get_symbols() or [settings.symbol]
    logger.info("Active symbols: %s", symbols)

    bias_filter = _build_bias_filter(db)

    def _build_orchestrator(sym: str) -> StrategyOrchestrator:
        rc = RiskConfig(risk_per_trade=settings.risk_per_trade)
        _apply_runtime_config(db, rc)
        orch = StrategyOrchestrator(
            db=db,
            symbol=sym,
            risk_config=rc,
            bias_filter=bias_filter,
            timeframe=settings.timeframe,
        )
        _apply_ema_config(db, orch)
        _init_quantity_precision(orch, db)
        return orch

    orchestrators: dict[str, StrategyOrchestrator] = {
        sym: _build_orchestrator(sym) for sym in symbols
    }
    primary_orch = orchestrators[symbols[0]]

    adaptor = ParameterAdaptor(
        db=db,
        risk_manager=primary_orch.risk_manager,
    )
    _init_price_precision(db)

    # Telegram — always instantiated; no-ops when unconfigured
    notifier    = TelegramNotifier(db=db)
    # Build the exchange client early so the command handler can fetch live prices
    stream_client = _build_client(db)

    # Lazy seed of account_baseline — deposit-immune HWM (idempotent; no-op after first run).
    # Must run AFTER the client is built so we can fetch the current USDT balance.
    # See design Decision 6 and gotcha #31.
    _init_account_baseline(db, stream_client)
    cmd_handler = TelegramCommandHandler(
        db=db,
        notifier=notifier,
        price_fetcher=lambda sym: stream_client.get_ticker_price(sym),
    )
    cmd_handler.start()

    # Start WebSocket price streams
    for sym in symbols:
        stream_client.start_price_stream(sym, _make_tick_handler(db, sym))

    logger.info(
        "Bot started — symbols=%s timeframe=%s dry_run=%s",
        symbols, settings.timeframe, args.dry_run,
    )
    notifier.bot_started(args.dry_run, db.get_active_mode())
    notifier.register_commands()

    def run_all_cycles() -> None:
        n = len(orchestrators)
        for sym, orch in orchestrators.items():
            try:
                run_cycle(orch, db, dry_run=args.dry_run, adaptor=adaptor, notifier=notifier, n_symbols=n)
            except Exception as exc:
                logger.error("run_cycle failed for %s: %s", sym, exc)

    # Run immediately on startup, then schedule hourly
    run_all_cycles()

    # Auto-optimizers: run on primary symbol only
    if should_run(db):
        _launch_auto_optimizer(db, primary_orch, notifier)
    if eq_should_run(db):
        _launch_auto_entry_quality_optimizer(db, primary_orch, notifier)

    schedule.every().hour.at(":00").do(run_all_cycles)
    schedule.every(60).seconds.do(
        position_manager, db, args.dry_run, primary_orch.risk_manager.config, notifier
    )
    schedule.every(7).days.do(
        _launch_auto_optimizer, db, primary_orch, notifier
    )
    schedule.every(7).days.do(
        _launch_auto_entry_quality_optimizer, db, primary_orch, notifier
    )

    while not _shutdown:
        if db.consume_restart_request():
            logger.info("Restart requested via dashboard — exiting for container restart.")
            break
        schedule.run_pending()
        time.sleep(10)

    notifier.bot_stopped()
    cmd_handler.stop()
    stream_client.stop_price_stream()
    logger.info("Bot stopped cleanly.")


if __name__ == "__main__":
    main()
