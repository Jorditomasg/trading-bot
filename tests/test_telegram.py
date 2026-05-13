"""Tests for TelegramNotifier and TelegramCommandHandler."""
from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pytest

from bot.telegram_notifier import TelegramNotifier
from bot.telegram_commands import TelegramCommandHandler


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _notifier(token="tok", chat_id="123", enabled=True) -> TelegramNotifier:
    """Return a TelegramNotifier backed by a mock DB."""
    db = MagicMock()
    db.get_telegram_config.return_value = {
        "token": token, "chat_id": chat_id, "enabled": enabled,
    }
    return TelegramNotifier(db)


def _closed_trades(n_wins: int = 3, n_losses: int = 1) -> list[dict]:
    trades = []
    for _ in range(n_wins):
        trades.append({"pnl": 100.0, "exit_price": 50000.0, "strategy": "EMA_CROSSOVER", "side": "BUY"})
    for _ in range(n_losses):
        trades.append({"pnl": -50.0, "exit_price": 49000.0, "strategy": "EMA_CROSSOVER", "side": "SELL"})
    return trades


def _equity_curve(start: float = 10000.0, end: float = 10350.0) -> list[dict]:
    return [
        {"timestamp": "2026-01-01T00:00:00", "balance": start, "drawdown": 0.0},
        {"timestamp": "2026-01-02T00:00:00", "balance": end,   "drawdown": 0.02},
    ]


def _perf_by_strategy() -> list[dict]:
    return [
        {
            "strategy": "EMA_CROSSOVER",
            "total_trades": 4,
            "wins": 3,
            "losses": 1,
            "win_rate": 75.0,
            "total_pnl": 250.0,
            "avg_pnl": 62.5,
        }
    ]


# ── status() ──────────────────────────────────────────────────────────────────

class TestStatus:
    def test_status_running_no_position(self):
        n = _notifier()
        with patch("bot.telegram_notifier._COMMA_DECIMAL", False), patch.object(n, "_post") as mock_post:
            n.status(10432.50, [], "TESTNET", paused=False)
        text = mock_post.call_args[0][0]
        assert "10,432.50" in text
        assert "Running" in text
        assert "No open positions" in text

    def test_status_paused(self):
        n = _notifier()
        with patch.object(n, "_post") as mock_post:
            n.status(10000.0, [], "TESTNET", paused=True)
        text = mock_post.call_args[0][0]
        assert "Paused" in text

    def test_status_with_open_position(self):
        n = _notifier()
        positions = [{
            "symbol": "BTCUSDT", "side": "BUY",
            "entry_price": 50000.0, "stop_loss": 49000.0, "take_profit": 52000.0,
            "current_price": None, "unrealized_pnl": None,
        }]
        with patch("bot.telegram_notifier._COMMA_DECIMAL", False), patch.object(n, "_post") as mock_post:
            n.status(10000.0, positions, "TESTNET", paused=False)
        text = mock_post.call_args[0][0]
        assert "BTCUSDT" in text
        assert "50,000.00" in text
        assert "49,000.00" in text
        assert "52,000.00" in text

    def test_status_with_multiple_positions(self):
        n = _notifier()
        positions = [
            {"symbol": "BTCUSDT", "side": "BUY", "entry_price": 50000.0,
             "stop_loss": 49000.0, "take_profit": 52000.0,
             "current_price": 51000.0, "unrealized_pnl": 12.34},
            {"symbol": "ETHUSDT", "side": "BUY", "entry_price": 3000.0,
             "stop_loss": 2900.0, "take_profit": 3200.0,
             "current_price": 3050.0, "unrealized_pnl": -1.5},
        ]
        with patch("bot.telegram_notifier._COMMA_DECIMAL", False), patch.object(n, "_post") as mock_post:
            n.status(10000.0, positions, "TESTNET", paused=False)
        text = mock_post.call_args[0][0]
        assert "BTCUSDT" in text
        assert "ETHUSDT" in text
        assert "+$12.34" in text
        assert "-$1.50" in text


# ── /status integration ───────────────────────────────────────────────────────

class TestStatusIntegration:
    def test_status_command_forwards_paused_state(self):
        db = MagicMock()
        db.get_telegram_config.return_value = {"token": "tok", "chat_id": "123", "enabled": True}
        db.get_equity_curve.return_value = [{"balance": 10000.0}]
        db.get_open_trades.return_value = []
        db.get_active_mode.return_value = "TESTNET"
        db.get_bot_paused.return_value = True
        notifier = MagicMock()
        handler = TelegramCommandHandler(db, notifier)
        update = {"update_id": 1, "message": {"chat": {"id": "123"}, "text": "/status"}}
        handler._handle(update, "123")
        notifier.status.assert_called_once_with(10000.0, [], "TESTNET", paused=True)


# ── register_commands() ───────────────────────────────────────────────────────

class TestRegisterCommands:
    def test_menu_only_lists_status_report_help(self):
        # The chat-UI menu deliberately hides /pause and /resume; they live
        # behind the /help inline keyboard. Anything else would clutter the
        # autocomplete and re-introduce the accidental-pause footgun.
        n = _notifier()
        with patch("requests.post") as mock_post:
            mock_post.return_value.raise_for_status = MagicMock()
            n.register_commands()
        assert mock_post.called
        payload = mock_post.call_args[1]["json"]
        commands = {c["command"] for c in payload["commands"]}
        assert commands == {"status", "report", "help"}

    def test_no_call_when_token_missing(self):
        n = _notifier(token="")
        with patch("requests.post") as mock_post:
            n.register_commands()
        mock_post.assert_not_called()

    def test_silently_ignores_http_error(self):
        n = _notifier()
        with patch("requests.post", side_effect=Exception("network error")):
            n.register_commands()  # must not raise


# ── report() ──────────────────────────────────────────────────────────────────

class TestReport:
    def test_no_trades_shows_no_data_message(self):
        n = _notifier()
        with patch.object(n, "_post") as mock_post:
            n.report([], [], [], 10000.0, "TESTNET", 10000.0)
        text = mock_post.call_args[0][0]
        assert "REPORT" in text
        assert "no" in text.lower()

    def test_with_trades_shows_all_metrics(self):
        n = _notifier()
        closed = _closed_trades(n_wins=3, n_losses=1)
        curve  = _equity_curve(start=10000.0, end=10350.0)
        perf   = _perf_by_strategy()
        with patch("bot.telegram_notifier._COMMA_DECIMAL", False), patch.object(n, "_post") as mock_post:
            n.report(closed, curve, perf, 10350.0, "TESTNET", 10000.0)
        text = mock_post.call_args[0][0]
        assert "75.0" in text or "75%" in text      # win rate
        assert "EMA_CROSSOVER" in text              # best strategy
        assert "10,350.00" in text                  # balance
        assert "Sharpe" in text                     # sharpe ratio label
        assert "drawdown" in text.lower()           # max drawdown label
        assert "streak" in text.lower()             # max loss streak label
        assert "Profit" in text                     # profit factor label

    def test_win_rate_calculation(self):
        n = _notifier()
        closed = _closed_trades(n_wins=1, n_losses=3)
        curve  = _equity_curve()
        with patch("bot.telegram_notifier._COMMA_DECIMAL", False), patch.object(n, "_post") as mock_post:
            n.report(closed, curve, [], 9850.0, "TESTNET", 10000.0)
        text = mock_post.call_args[0][0]
        assert "25.0" in text      # 1/4 = 25% win rate

    def test_positive_pnl_shows_plus_sign(self):
        n = _notifier()
        closed = _closed_trades(n_wins=2, n_losses=0)
        curve  = _equity_curve(start=10000.0, end=10200.0)
        with patch.object(n, "_post") as mock_post:
            n.report(closed, curve, [], 10200.0, "TESTNET", 10000.0)
        text = mock_post.call_args[0][0]
        assert "+" in text

    def test_mainnet_tag_present(self):
        n = _notifier()
        with patch.object(n, "_post") as mock_post:
            n.report([], [], [], 10000.0, "MAINNET", 10000.0)
        text = mock_post.call_args[0][0]
        assert "MAINNET" in text


# ── TelegramCommandHandler ────────────────────────────────────────────────────

def _handler() -> tuple[TelegramCommandHandler, MagicMock, MagicMock]:
    """Return (handler, mock_db, mock_notifier)."""
    db       = MagicMock()
    notifier = MagicMock()
    db.get_telegram_config.return_value = {"token": "tok", "chat_id": "123", "enabled": True}
    handler  = TelegramCommandHandler(db, notifier)
    return handler, db, notifier


def _update(text: str, chat_id: str = "123") -> dict:
    return {
        "update_id": 1,
        "message": {"chat": {"id": chat_id}, "text": text},
    }


class TestCommandHandler:
    def test_report_calls_notifier_report(self):
        h, db, notifier = _handler()
        db.get_all_trades.return_value              = []
        db.get_equity_curve.return_value            = []
        db.get_performance_by_strategy.return_value = []
        db.get_symbols.return_value                 = []
        db.get_active_mode.return_value             = "TESTNET"
        h._handle(_update("/report"), "123")
        notifier.report.assert_called_once()
        args = notifier.report.call_args[0]
        assert len(args) == 6                    # all 6 positional args passed
        assert isinstance(args[5], float)        # initial_capital is a float

    def test_report_filters_closed_trades(self):
        h, db, notifier = _handler()
        db.get_all_trades.return_value = [
            {"pnl": 100.0, "exit_price": 50000.0},   # closed
            {"pnl": None,  "exit_price": None},       # open — must be excluded
        ]
        db.get_equity_curve.return_value            = []
        db.get_performance_by_strategy.return_value = []
        db.get_symbols.return_value                 = []
        db.get_active_mode.return_value             = "TESTNET"
        h._handle(_update("/report"), "123")
        closed_arg = notifier.report.call_args[0][0]
        assert len(closed_arg) == 1

    def test_report_with_symbol_arg_filters_db_query(self):
        h, db, notifier = _handler()
        db.get_all_trades.return_value              = []
        db.get_equity_curve.return_value            = []
        db.get_performance_by_strategy.return_value = []
        db.get_symbols.return_value                 = ["BTCUSDT", "ETHUSDT"]
        db.get_active_mode.return_value             = "TESTNET"
        h._handle(_update("/report ETHUSDT"), "123")
        # DB queries filtered by symbol
        db.get_all_trades.assert_called_with(symbol="ETHUSDT")
        db.get_performance_by_strategy.assert_called_with(symbol="ETHUSDT")
        # Notifier received the symbol kwarg
        kwargs = notifier.report.call_args[1]
        assert kwargs.get("symbol") == "ETHUSDT"

    def test_report_unknown_symbol_aborts_with_hint(self):
        h, db, notifier = _handler()
        db.get_symbols.return_value     = ["BTCUSDT"]
        db.get_active_mode.return_value = "TESTNET"
        h._handle(_update("/report DOGEUSDT"), "123")
        notifier.report.assert_not_called()

    def test_unknown_chat_id_ignored(self):
        h, db, notifier = _handler()
        h._handle(_update("/report", chat_id="999"), "123")
        notifier.report.assert_not_called()

    def test_pause_still_works(self):
        h, db, notifier = _handler()
        h._handle(_update("/pause"), "123")
        db.set_bot_paused.assert_called_once_with(True)
        notifier.paused.assert_called_once()

    def test_status_passes_paused_flag(self):
        h, db, notifier = _handler()
        db.get_equity_curve.return_value  = [{"balance": 10000.0}]
        db.get_open_trades.return_value   = []
        db.get_bot_paused.return_value    = True
        db.get_active_mode.return_value   = "TESTNET"
        h._handle(_update("/status"), "123")
        notifier.status.assert_called_once()
        _, kwargs = notifier.status.call_args
        assert kwargs.get("paused") is True

    def test_help_command_invokes_notifier_help(self):
        h, _, notifier = _handler()
        h._handle(_update("/help"), "123")
        notifier.help.assert_called_once()

    def test_start_command_also_shows_help(self):
        # /start is the conventional first message in any Telegram bot.
        # Routing it to the help menu gives new chats an obvious entry point.
        h, _, notifier = _handler()
        h._handle(_update("/start"), "123")
        notifier.help.assert_called_once()


class TestCallbackRouting:
    def _callback_update(self, data: str, chat_id: str = "123", cb_id: str = "cb1") -> dict:
        return {
            "update_id": 2,
            "callback_query": {
                "id": cb_id,
                "data": data,
                "message": {"chat": {"id": chat_id}},
            },
        }

    def test_pause_button_press_pauses_bot(self):
        h, db, notifier = _handler()
        with patch("requests.post"):  # swallow answerCallbackQuery
            h._handle_callback(
                self._callback_update("/pause")["callback_query"], "123", "tok",
            )
        db.set_bot_paused.assert_called_once_with(True)
        notifier.paused.assert_called_once()

    def test_resume_button_press_resumes_bot(self):
        h, db, notifier = _handler()
        with patch("requests.post"):
            h._handle_callback(
                self._callback_update("/resume")["callback_query"], "123", "tok",
            )
        db.set_bot_paused.assert_called_once_with(False)
        notifier.resumed.assert_called_once()

    def test_status_button_press_routes_through_handle(self):
        h, db, notifier = _handler()
        db.get_equity_curve.return_value = [{"balance": 10000.0}]
        db.get_open_trades.return_value  = []
        db.get_bot_paused.return_value   = False
        db.get_active_mode.return_value  = "TESTNET"
        with patch("requests.post"):
            h._handle_callback(
                self._callback_update("/status")["callback_query"], "123", "tok",
            )
        notifier.status.assert_called_once()

    def test_callback_from_unknown_chat_ignored(self):
        h, db, notifier = _handler()
        with patch("requests.post"):
            h._handle_callback(
                self._callback_update("/pause", chat_id="999")["callback_query"],
                "123", "tok",
            )
        db.set_bot_paused.assert_not_called()
        notifier.paused.assert_not_called()

    def test_callback_always_answered_even_on_handler_error(self):
        # answerCallbackQuery MUST fire even when the inner command raises,
        # otherwise the button stays spinning forever in the user's chat.
        h, db, notifier = _handler()
        notifier.paused.side_effect = RuntimeError("boom")
        with patch("requests.post") as mock_post:
            with pytest.raises(RuntimeError):
                h._handle_callback(
                    self._callback_update("/pause", cb_id="abc")["callback_query"],
                    "123", "tok",
                )
        # Find the answerCallbackQuery call among any other requests.post calls
        urls = [c.args[0] for c in mock_post.call_args_list]
        assert any("answerCallbackQuery" in u for u in urls)


# ── /reset_hwm command (Task 10 — RED scaffold) ───────────────────────────────

class TestResetHWMCommand:
    """/reset_hwm command handler tests.

    Written RED-first (Task 10). GREEN pass when Tasks 11-12 implementations land.
    Uses mocked DB and notifier (consistent with the rest of this test file).
    """

    def test_reset_hwm_no_args_resets_to_current_equity(self):
        """/reset_hwm with no args calls reset_peak_capital(value=None, clear_breaker=True)."""
        h, db, notifier = _handler()
        db.reset_peak_capital.return_value = (39277.0, 18625.0)
        db.get_active_mode.return_value = "TESTNET"

        h._handle(_update("/reset_hwm"), "123")

        db.reset_peak_capital.assert_called_once_with(value=None, clear_breaker=True)
        notifier.hwm_reset.assert_called_once_with(39277.0, 18625.0, "TESTNET")

    def test_reset_hwm_with_value_sets_explicit_peak(self):
        """/reset_hwm 18625 calls reset_peak_capital(value=18625.0, clear_breaker=True)."""
        h, db, notifier = _handler()
        db.reset_peak_capital.return_value = (39277.0, 18625.0)
        db.get_active_mode.return_value = "TESTNET"

        h._handle(_update("/reset_hwm 18625"), "123")

        db.reset_peak_capital.assert_called_once_with(value=18625.0, clear_breaker=True)
        notifier.hwm_reset.assert_called_once_with(39277.0, 18625.0, "TESTNET")

    def test_reset_hwm_invalid_arg_returns_error_message(self):
        """/reset_hwm abc posts an error message; no reset called."""
        h, db, notifier = _handler()

        h._handle(_update("/reset_hwm abc"), "123")

        db.reset_peak_capital.assert_not_called()
        notifier.hwm_reset.assert_not_called()
        # _post should have been called with an error message
        notifier._post.assert_called_once()
        err_msg = notifier._post.call_args[0][0]
        assert "abc" in err_msg or "invalid" in err_msg.lower() or "usage" in err_msg.lower()

    def test_reset_hwm_clears_breaker_timestamps(self):
        """/reset_hwm with clear_breaker=True removes breaker rows.

        Uses a real Database(tmp_path) to verify the DB-level DELETE works end-to-end.
        """
        import tempfile
        import os
        from bot.database.db import Database as RealDatabase

        with tempfile.TemporaryDirectory() as tmpdir:
            real_db = RealDatabase(os.path.join(tmpdir, "test.db"))
            real_db.set_peak_capital(39277.0)
            real_db.set_account_baseline(18625.0)
            real_db.set_config("breaker_triggered_at_BTCUSDT", "2026-05-13T10:00:00")
            real_db.set_config("breaker_triggered_at_ETHUSDT", "2026-05-13T10:00:00")

            mock_notifier = MagicMock()
            mock_notifier._post = MagicMock()
            mock_notifier.hwm_reset = MagicMock()

            handler = TelegramCommandHandler(real_db, mock_notifier)
            handler._handle(_update("/reset_hwm"), "123")

            # Breaker keys must be gone after /reset_hwm
            assert real_db.get_config("breaker_triggered_at_BTCUSDT") in (None, "")
            assert real_db.get_config("breaker_triggered_at_ETHUSDT") in (None, "")

    def test_reset_hwm_unauthorized_chat_id_ignored(self):
        """/reset_hwm from unauthorized chat_id is silently ignored."""
        h, db, notifier = _handler()

        # Send from chat_id "999" — allowed_chat_id is "123"
        h._handle(_update("/reset_hwm", chat_id="999"), "123")

        db.reset_peak_capital.assert_not_called()
        notifier.hwm_reset.assert_not_called()


# ── hwm_reset() notifier method (Task 10 — RED scaffold) ──────────────────────

class TestHwmResetNotifier:
    """Tests for TelegramNotifier.hwm_reset().

    Written RED-first (Task 10). GREEN pass when Task 11 implementation lands.
    """

    def test_hwm_reset_sends_message_with_old_and_new_peak(self):
        """hwm_reset() must send a message containing both old and new peak values."""
        n = _notifier()
        with patch.object(n, "_post") as mock_post:
            n.hwm_reset(39277.0, 18625.0, "TESTNET")
        assert mock_post.called
        text = mock_post.call_args[0][0]
        # Allow for locale-aware formatting: 39,277.00 (dot) or 39.277,00 (comma)
        assert "39" in text and "277" in text
        assert "18" in text and "625" in text

    def test_hwm_reset_includes_mode_tag(self):
        """hwm_reset() message must include the mode tag (DEMO or MAINNET)."""
        n = _notifier()
        with patch.object(n, "_post") as mock_post:
            n.hwm_reset(39277.0, 18625.0, "TESTNET")
        text = mock_post.call_args[0][0]
        assert "DEMO" in text or "TESTNET" in text

    def test_hwm_reset_mentions_breaker_cleared(self):
        """hwm_reset() message must mention that breaker timers are cleared."""
        n = _notifier()
        with patch.object(n, "_post") as mock_post:
            n.hwm_reset(39277.0, 18625.0, "TESTNET")
        text = mock_post.call_args[0][0]
        assert "breaker" in text.lower() or "timer" in text.lower() or "resume" in text.lower()
