"""Multi-symbol portfolio backtest engine — shared USDT cash pool across symbols.

Simulates the multi-symbol bot pipeline with one shared USDT cash pool. Each
symbol owns a dedicated `BacktestEngine` instance for signal generation, exit
detection, cost application and risk-based sizing — but capital flows through
a single pool. Position sizing reads the live cash balance (matching the live
bot which calls `BinanceClient.get_balance("USDT")`), NOT the marked-to-market
total equity.

The simulation walks the UNION of all symbols' bar timestamps in ascending
order. On each timestamp, every symbol that has a bar at exactly that moment
participates: PASS 1 closes existing positions if SL / TP / liquidation hit;
PASS 2 opens at most one new position per symbol (mirroring the live bot's
`max_concurrent_trades=1` per symbol). Cooldown counters tick once per bar a
symbol participates in.

The portfolio equity curve is computed as cash + the sum of unrealised PnL of
every open position at the end of each timestamp.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from bot.backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    BacktestResult,
    EXIT_END_OF_PERIOD,
    EXIT_LIQUIDATED,
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    _OHLCV_COLS,
)
from bot.bias.filter import BiasFilter, BiasFilterConfig
from bot.constants import StrategyName
from bot.metrics import (
    max_drawdown,
    profit_factor,
    sharpe_ratio,
)
from bot.risk.drawdown_scaler import DrawdownRiskConfig, drawdown_multiplier

logger = logging.getLogger(__name__)


@dataclass
class _SymbolState:
    """Mutable per-symbol simulation state held by the portfolio loop."""
    symbol:        str
    engine:        BacktestEngine
    df:            pd.DataFrame
    df_4h:         pd.DataFrame | None
    df_weekly:     pd.DataFrame | None
    df_1m:         pd.DataFrame | None
    min_lookback:  int
    open_trade:    dict | None         = None
    cooldown_bars: int                 = 0
    trades:        list[dict]          = field(default_factory=list)
    last_close:    float               = 0.0


@dataclass
class PortfolioBacktestResult:
    combined_equity_curve: list[dict]
    per_symbol_trades:     dict[str, list[dict]]
    per_symbol_summary:    dict[str, dict]
    portfolio_summary:     dict
    start_date:            str
    end_date:              str
    symbols:               list[str]
    timeframe:             str
    initial_capital:       float
    final_capital:         float


class PortfolioBacktestEngine:
    """Simulate the multi-symbol bot pipeline with a shared USDT cash pool.

    Each symbol drives its own `BacktestEngine` instance for signal / exit /
    sizing logic, but capital is owned by the portfolio.  Maximum one open
    position per symbol — matching the live bot constraint where each symbol
    has its own orchestrator with `max_concurrent_trades=1`.
    """

    def __init__(self, config: BacktestConfig = BacktestConfig()) -> None:
        self.config = config

    # ── Public API ────────────────────────────────────────────────────────────

    def run_portfolio(
        self,
        dfs:        dict[str, pd.DataFrame],
        dfs_4h:     dict[str, pd.DataFrame] | None = None,
        dfs_weekly: dict[str, pd.DataFrame] | None = None,
        dfs_1m:     dict[str, pd.DataFrame] | None = None,
    ) -> PortfolioBacktestResult:
        """Simulate every symbol concurrently over the union of their timestamps.

        Args:
            dfs:        symbol → primary OHLCV DataFrame (sorted ascending).
            dfs_4h:     symbol → 4h DataFrame for BiasFilter (optional).
            dfs_weekly: symbol → weekly DataFrame for momentum filter (optional).
            dfs_1m:     symbol → 1m DataFrame for precise exit detection (optional).

        Returns:
            PortfolioBacktestResult with combined equity, per-symbol trades and
            per-symbol metrics.
        """
        if not dfs:
            raise ValueError("dfs must contain at least one symbol")

        dfs_4h     = dfs_4h     or {}
        dfs_weekly = dfs_weekly or {}
        dfs_1m     = dfs_1m     or {}

        states: dict[str, _SymbolState] = {}
        for symbol, df in dfs.items():
            engine = BacktestEngine(self.config)
            engine._validate_inputs(df, self.config)
            df_norm = engine._normalize_timestamps(df)

            df_4h_norm     = engine._normalize_timestamps(dfs_4h[symbol])     if symbol in dfs_4h     and dfs_4h[symbol]     is not None else None
            df_weekly_norm = engine._normalize_timestamps(dfs_weekly[symbol]) if symbol in dfs_weekly and dfs_weekly[symbol] is not None else None
            df_1m_norm     = engine._normalize_timestamps(dfs_1m[symbol])     if symbol in dfs_1m     and dfs_1m[symbol]     is not None else None
            if df_1m_norm is not None and not df_1m_norm.empty:
                df_1m_norm = df_1m_norm.sort_values("open_time").reset_index(drop=True)

            # Mirror BacktestEngine.run() — disable BiasFilter when no 4h data is
            # available so the filter doesn't block all signals as NEUTRAL.
            if df_4h_norm is None:
                engine._bias_filter = BiasFilter(BiasFilterConfig(enabled=False))

            min_lb = engine._min_lookback()
            if len(df_norm) <= min_lb:
                raise ValueError(
                    f"Insufficient data for {symbol}: {len(df_norm)} bars, "
                    f"need > {min_lb} (timeframe={self.config.timeframe})"
                )

            states[symbol] = _SymbolState(
                symbol       = symbol,
                engine       = engine,
                df           = df_norm,
                df_4h        = df_4h_norm,
                df_weekly    = df_weekly_norm,
                df_1m        = df_1m_norm,
                min_lookback = min_lb,
                last_close   = float(df_norm.iloc[0]["close"]),
            )

        # Build the union of all symbol timestamps, sorted ascending.
        all_times = pd.Index([])
        for state in states.values():
            all_times = all_times.union(pd.Index(state.df["open_time"]))
        union_times = list(all_times.sort_values())

        capital      = self.config.initial_capital
        peak_capital = capital  # HWM for drawdown-aware risk scaling
        dd_cfg       = self.config.dd_risk if self.config.dd_risk is not None else DrawdownRiskConfig(enabled=False)
        equity_curve: list[dict] = []

        for current_time in union_times:
            participating: dict[str, int] = {}
            for symbol, state in states.items():
                idx = int(state.df["open_time"].searchsorted(current_time, side="left"))
                if idx >= len(state.df):
                    continue
                if state.df.iloc[idx]["open_time"] != current_time:
                    continue
                if idx < state.min_lookback:
                    # Symbol has a bar at this T but it's still inside warmup —
                    # update last_close so the equity curve tracks price moves.
                    state.last_close = float(state.df.iloc[idx]["close"])
                    continue
                participating[symbol] = idx

            # ── PASS 1: exits on open positions ───────────────────────────────
            for symbol, idx in participating.items():
                state = states[symbol]
                if state.open_trade is None:
                    continue

                bar    = state.df.iloc[idx]
                engine = state.engine

                liq_info = engine._check_liquidation(state.open_trade, bar)
                if liq_info is not None:
                    _, liq_price        = liq_info
                    margin              = state.open_trade["entry_price"] * state.open_trade["quantity"] / self.config.leverage
                    notional            = state.open_trade["entry_price"] * state.open_trade["quantity"]
                    pnl                 = -margin
                    capital            += pnl
                    peak_capital        = max(peak_capital, capital)

                    state.open_trade["exit_price"]  = liq_price
                    state.open_trade["exit_reason"] = EXIT_LIQUIDATED
                    state.open_trade["exit_bar"]    = idx
                    state.open_trade["exit_time"]   = current_time
                    state.open_trade["pnl"]         = pnl
                    state.open_trade["pnl_pct"]     = pnl / notional if notional > 0 else -1.0

                    state.trades.append(state.open_trade)
                    state.open_trade    = None
                    state.cooldown_bars = self.config.post_close_cooldown_bars
                    continue

                exit_info = self._exit_info_for(state, idx, bar)
                if exit_info is not None:
                    reason, raw_exit       = exit_info
                    net_exit               = engine._apply_exit_cost(state.open_trade["side"], raw_exit)
                    bars_held              = idx - state.open_trade["entry_bar"]
                    raw_pnl                = engine._compute_pnl(state.open_trade, net_exit)
                    pnl                    = engine._apply_leverage(raw_pnl, state.open_trade, bars_held)
                    notional               = state.open_trade["entry_price"] * state.open_trade["quantity"]
                    capital               += pnl
                    peak_capital           = max(peak_capital, capital)

                    state.open_trade["exit_price"]  = net_exit
                    state.open_trade["exit_reason"] = reason
                    state.open_trade["exit_bar"]    = idx
                    state.open_trade["exit_time"]   = current_time
                    state.open_trade["pnl"]         = pnl
                    state.open_trade["pnl_pct"]     = pnl / notional if notional > 0 else 0.0

                    state.trades.append(state.open_trade)
                    state.open_trade    = None
                    state.cooldown_bars = self.config.post_close_cooldown_bars

            # ── Tick cooldown counters once per participating bar ─────────────
            for symbol in participating:
                state = states[symbol]
                if state.cooldown_bars > 0:
                    state.cooldown_bars -= 1

            # ── PASS 2: entries ───────────────────────────────────────────────
            for symbol, idx in participating.items():
                state = states[symbol]
                if state.open_trade is not None or state.cooldown_bars != 0:
                    continue

                engine = state.engine
                bar    = state.df.iloc[idx]

                window         = state.df.iloc[: idx + 1][_OHLCV_COLS].reset_index(drop=True)
                window_4h      = engine._get_4h_window(state.df_4h, current_time)
                weekly_window  = engine._get_weekly_window(state.df_weekly, current_time)
                momentum_state = engine._get_momentum_state(weekly_window)

                regime, signal = engine._generate_signal(window, window_4h)

                # Vol-regime filter — opt-in per engine config (mirrors engine.py:655-658)
                vol_state       = engine._vol_filter.get_state(window)
                vol_allows      = engine._vol_filter.allows_signal(vol_state)
                vol_size_factor = engine._vol_filter.size_factor(vol_state)

                if (
                    signal.action == "HOLD"
                    or signal.strength < self.config.min_signal_strength
                    or signal.stop_loss is None
                    or signal.stop_loss <= 0
                    or momentum_state == "BEARISH"
                    or not vol_allows
                ):
                    continue

                raw_entry = float(bar["close"])
                net_entry = engine._apply_entry_cost(signal.action, raw_entry)
                effective_risk = (
                    self.config.risk_per_trade * 0.5
                    if momentum_state == "NEUTRAL"
                    else self.config.risk_per_trade
                )
                # Vol-regime size scaling (active only when action=reduce + LOW_VOL)
                effective_risk = effective_risk * vol_size_factor
                # Drawdown-aware risk scaling (active only when dd_risk config enabled)
                effective_risk = effective_risk * drawdown_multiplier(
                    capital, peak_capital, dd_cfg
                )
                # NOTE: sizing uses CASH (capital), not total equity — mirrors the
                # live bot's `BinanceClient.get_balance("USDT")` semantics.
                quantity = engine._compute_quantity_with_risk(
                    capital, net_entry, signal.stop_loss, effective_risk
                )

                if quantity <= 0:
                    continue

                state.open_trade = {
                    "entry_bar":   idx,
                    "entry_time":  current_time,
                    "side":        signal.action,
                    "entry_price": net_entry,
                    "stop_loss":   signal.stop_loss,
                    "take_profit": signal.take_profit,
                    "quantity":    quantity,
                    "atr":         signal.atr,
                    "strategy":    StrategyName.EMA_CROSSOVER.value,
                    "regime":      regime.value,
                    "symbol":      symbol,
                    # filled on close:
                    "exit_bar":    None,
                    "exit_time":   None,
                    "exit_price":  None,
                    "exit_reason": None,
                    "pnl":         None,
                    "pnl_pct":     None,
                }

            # ── Update last_close for every participating symbol ──────────────
            for symbol, idx in participating.items():
                state = states[symbol]
                state.last_close = float(state.df.iloc[idx]["close"])

            # ── Equity snapshot — cash + sum of unrealised PnL ────────────────
            combined = capital + sum(self._unrealized(s) for s in states.values())
            equity_curve.append({"time": str(current_time), "balance": combined})

        # ── Force-close any still-open position at its symbol's last bar ──────
        for symbol, state in states.items():
            if state.open_trade is None:
                continue
            engine    = state.engine
            last_idx  = len(state.df) - 1
            last_bar  = state.df.iloc[last_idx]
            raw_exit  = float(last_bar["close"])
            net_exit  = engine._apply_exit_cost(state.open_trade["side"], raw_exit)
            bars_held = last_idx - state.open_trade["entry_bar"]
            raw_pnl   = engine._compute_pnl(state.open_trade, net_exit)
            pnl       = engine._apply_leverage(raw_pnl, state.open_trade, bars_held)
            notional  = state.open_trade["entry_price"] * state.open_trade["quantity"]
            capital  += pnl
            peak_capital = max(peak_capital, capital)

            state.open_trade["exit_price"]  = net_exit
            state.open_trade["exit_reason"] = EXIT_END_OF_PERIOD
            state.open_trade["exit_bar"]    = last_idx
            state.open_trade["exit_time"]   = last_bar["open_time"]
            state.open_trade["pnl"]         = pnl
            state.open_trade["pnl_pct"]     = pnl / notional if notional > 0 else 0.0

            state.trades.append(state.open_trade)
            state.open_trade = None
            state.last_close = raw_exit

        # Final equity snapshot reflects closed positions.
        if equity_curve:
            equity_curve[-1] = {"time": equity_curve[-1]["time"], "balance": capital}
        else:
            # No timestamps were processed (e.g. all symbols below min_lookback).
            equity_curve.append({"time": "", "balance": capital})

        # ── Per-symbol summaries via the shared BacktestEngine helper ─────────
        # Each symbol gets its own derived equity curve (initial + cumulative
        # realised PnL of its own trades) so total_pnl, Sharpe and MaxDD are
        # symbol-specific instead of mirroring the portfolio total.
        per_symbol_trades:  dict[str, list[dict]] = {s: st.trades for s, st in states.items()}
        per_symbol_summary: dict[str, dict]       = {}
        for symbol, state in states.items():
            symbol_start = state.df.iloc[state.min_lookback]["open_time"].strftime("%Y-%m-%d")
            symbol_end   = state.df.iloc[-1]["open_time"].strftime("%Y-%m-%d")

            initial      = self.config.initial_capital
            running      = initial
            sym_equity: list[dict] = [
                {"bar": 0, "time": str(state.df.iloc[state.min_lookback]["open_time"]), "balance": running}
            ]
            for t in state.trades:
                running += t.get("pnl") or 0.0
                sym_equity.append({
                    "bar":     t.get("exit_bar")  or 0,
                    "time":    str(t.get("exit_time") or ""),
                    "balance": running,
                })
            sym_final = running

            sym_result   = BacktestResult(
                trades          = state.trades,
                equity_curve    = sym_equity,
                initial_capital = initial,
                final_capital   = sym_final,
                timeframe       = self.config.timeframe,
                symbol          = symbol,
                start_date      = symbol_start,
                end_date        = symbol_end,
                total_bars      = len(state.df) - state.min_lookback,
            )
            per_symbol_summary[symbol] = state.engine.summary(sym_result)

        # ── Portfolio-level summary across all closed trades ──────────────────
        all_closed = [
            t
            for trades in per_symbol_trades.values()
            for t in trades
            if t["exit_reason"] != EXIT_END_OF_PERIOD
        ]
        wins         = [t for t in all_closed if (t.get("pnl") or 0) > 0]
        total_trades = len(all_closed)
        win_rate_pct = len(wins) / total_trades * 100 if total_trades > 0 else 0.0

        tf_h          = BacktestEngine._timeframe_hours(self.config.timeframe)
        total_pnl     = capital - self.config.initial_capital
        total_pnl_pct = total_pnl / self.config.initial_capital * 100 if self.config.initial_capital > 0 else 0.0

        portfolio_summary = {
            "total_trades":     total_trades,
            "total_pnl":        total_pnl,
            "total_pnl_pct":    total_pnl_pct,
            "sharpe_ratio":     sharpe_ratio(equity_curve, timeframe_hours=tf_h),
            "max_drawdown_pct": max_drawdown(equity_curve) * 100,
            "profit_factor":    profit_factor(all_closed),
            "win_rate_pct":     win_rate_pct,
        }

        if union_times:
            start_date = pd.Timestamp(union_times[0]).strftime("%Y-%m-%d")
            end_date   = pd.Timestamp(union_times[-1]).strftime("%Y-%m-%d")
        else:
            start_date = end_date = ""

        return PortfolioBacktestResult(
            combined_equity_curve = equity_curve,
            per_symbol_trades     = per_symbol_trades,
            per_symbol_summary    = per_symbol_summary,
            portfolio_summary     = portfolio_summary,
            start_date            = start_date,
            end_date              = end_date,
            symbols               = list(states.keys()),
            timeframe             = self.config.timeframe,
            initial_capital       = self.config.initial_capital,
            final_capital         = capital,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _unrealized(state: _SymbolState) -> float:
        """Mark-to-market unrealised PnL of an open position, or 0 if flat."""
        trade = state.open_trade
        if trade is None:
            return 0.0
        qty   = trade["quantity"]
        entry = trade["entry_price"]
        last  = state.last_close
        if trade["side"] == "BUY":
            return (last - entry) * qty
        return (entry - last) * qty

    @staticmethod
    def _exit_info_for(
        state: _SymbolState, idx: int, bar: pd.Series
    ) -> tuple[str, float] | None:
        """Resolve SL/TP exit using 1m precision when df_1m is available."""
        engine = state.engine
        if state.df_1m is not None and not state.df_1m.empty:
            current_time = bar["open_time"]
            next_time = (
                state.df.iloc[idx + 1]["open_time"]
                if idx + 1 < len(state.df)
                else pd.Timestamp.max.tz_localize("UTC")
            )
            lo = int(state.df_1m["open_time"].searchsorted(current_time, side="left"))
            hi = int(state.df_1m["open_time"].searchsorted(next_time,   side="left"))
            m1_slice = state.df_1m.iloc[lo:hi]
            if len(m1_slice) > 0:
                return engine._check_exit_precise(state.open_trade, m1_slice)
        return engine._check_exit(state.open_trade, bar)
