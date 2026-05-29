"""Tests for the daily Telegram heartbeat digest.

Covers the three pieces:
  - _holding_reason()  — pure reason derivation (main.py)
  - TelegramNotifier.heartbeat()  — message formatting
  - StrategyOrchestrator.last_* snapshot — populated by step()
  - send_heartbeat() — gathers DB + orchestrator state and calls the notifier
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

import main as main_module
from bot.bias.filter import BiasFilter, BiasFilterConfig
from bot.database.db import Database
from bot.orchestrator import StrategyOrchestrator
from bot.telegram_notifier import TelegramNotifier
from tests.conftest import flat, uptrend


# ── _holding_reason ─────────────────────────────────────────────────────────

class TestHoldingReason:
    def test_open_position_takes_priority(self):
        assert main_module._holding_reason("TRENDING", "BULLISH", "BUY", True) == "en posición abierta"

    def test_active_signal(self):
        assert "BUY" in main_module._holding_reason("TRENDING", "BULLISH", "BUY", False)

    def test_non_trending_regime(self):
        r = main_module._holding_reason("RANGING", "BULLISH", "HOLD", False)
        assert "RANGING" in r and "TRENDING" in r

    def test_bearish_bias_long_only(self):
        r = main_module._holding_reason("TRENDING", "BEARISH", "HOLD", False)
        assert "bajista" in r

    def test_neutral_bias_data_gap(self):
        r = main_module._holding_reason("TRENDING", "NEUTRAL", "HOLD", False)
        assert "NEUTRAL" in r

    def test_default_waiting(self):
        assert main_module._holding_reason(None, None, "HOLD", False) == "esperando setup"


# ── TelegramNotifier.heartbeat ──────────────────────────────────────────────

def _notifier() -> TelegramNotifier:
    db = MagicMock()
    db.get_telegram_config.return_value = {"token": "t", "chat_id": "1", "enabled": True}
    return TelegramNotifier(db)


class TestHeartbeatMessage:
    def test_renders_equity_drawdown_and_per_symbol(self):
        n = _notifier()
        with patch("bot.telegram_notifier._COMMA_DECIMAL", False), patch.object(n, "_post") as post:
            n.heartbeat(
                equity=17985.87,
                drawdown=0.0096,
                open_positions=[],
                per_symbol=[
                    {"symbol": "BTCUSDT", "regime": "TRENDING", "bias": "BEARISH",
                     "reason": "esperando: bias bajista (long-only no compra)"},
                ],
                mode="TESTNET",
                paused=False,
                trades_24h=0,
            )
        text = post.call_args[0][0]
        assert "HEARTBEAT" in text
        assert "17,985.87" in text
        assert "0.96%" in text
        assert "Running" in text
        assert "DEMO" in text          # TESTNET → 🧪 DEMO tag
        assert "BTCUSDT" in text
        assert "BEARISH" in text
        assert "bajista" in text

    def test_paused_and_open_count(self):
        n = _notifier()
        with patch.object(n, "_post") as post:
            n.heartbeat(
                equity=10000.0, drawdown=0.0,
                open_positions=[{"symbol": "BTCUSDT"}],
                per_symbol=[],
                mode="MAINNET", paused=True, trades_24h=2,
            )
        text = post.call_args[0][0]
        assert "Paused" in text
        assert "MAINNET" in text
        assert "Open: <code>1</code>" in text
        assert "No active symbols" in text


# ── Orchestrator snapshot ───────────────────────────────────────────────────

class TestOrchestratorSnapshot:
    def test_step_populates_last_decision(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        orch = StrategyOrchestrator(db=db, symbol="BTCUSDT", timeframe="4h")
        # flat market → not TRENDING → HOLD, but the snapshot must still be set.
        orch.step(flat(300, freq="4h"), 10000.0)
        assert orch.last_regime is not None
        assert orch.last_action == "HOLD"

    def test_step_records_bias_when_filter_present(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        bias = BiasFilter(BiasFilterConfig(neutral_passthrough=False))
        orch = StrategyOrchestrator(db=db, symbol="BTCUSDT", timeframe="4h", bias_filter=bias)
        df = uptrend(300, freq="4h")
        df_high = uptrend(60, freq="1d")
        orch.step(df, 10000.0, df_high=df_high)
        assert orch.last_bias is not None  # bias was computed and stored


# ── send_heartbeat wiring ───────────────────────────────────────────────────

def _insert_closed_trade(db: Database, symbol: str, pnl: float, exit_time: str):
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO trades
               (symbol, side, strategy, regime, entry_price, exit_price, quantity,
                pnl, entry_time, exit_time, stop_loss, take_profit, exit_reason)
               VALUES (?, 'BUY', 'EMA_CROSSOVER', 'TRENDING', 100.0, 95.0, 1.0,
                       ?, ?, ?, 90.0, 120.0, 'STOP_LOSS')""",
            (symbol, pnl, exit_time, exit_time),
        )


class TestSendHeartbeat:
    def test_gathers_state_and_calls_notifier(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        db.set_account_baseline(18000.0)
        db.set_peak_capital(18000.0)
        # One closed loss within 24h → equity below baseline, trades_24h == 1
        recent = dt.datetime.now().isoformat()
        _insert_closed_trade(db, "BTCUSDT", -50.0, recent)

        btc = MagicMock(); btc.last_regime = MagicMock(value="TRENDING"); btc.last_bias = MagicMock(value="BEARISH"); btc.last_action = "HOLD"
        eth = MagicMock(); eth.last_regime = MagicMock(value="TRENDING"); eth.last_bias = MagicMock(value="BEARISH"); eth.last_action = "HOLD"
        orchestrators = {"BTCUSDT": btc, "ETHUSDT": eth}

        notifier = MagicMock()
        main_module.send_heartbeat(orchestrators, db, notifier)

        notifier.heartbeat.assert_called_once()
        kwargs = notifier.heartbeat.call_args.kwargs
        assert kwargs["equity"] == pytest.approx(17950.0)   # 18000 - 50
        assert kwargs["trades_24h"] == 1
        assert {s["symbol"] for s in kwargs["per_symbol"]} == {"BTCUSDT", "ETHUSDT"}
        assert all("bajista" in s["reason"] for s in kwargs["per_symbol"])

    def test_swallows_errors(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        notifier = MagicMock()
        # Orchestrator with a broken attribute access should not raise out of send_heartbeat
        bad = MagicMock()
        type(bad).last_regime = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
        main_module.send_heartbeat({"BTCUSDT": bad}, db, notifier)
        notifier.heartbeat.assert_not_called()
