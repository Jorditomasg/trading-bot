"""Backtest engine — simulates the live bot bar by bar over historical OHLCV data.

Design decisions:
- Entry at the close of the signal bar (realistic for hourly timeframes).
- SL/TP exits use the bar's high/low so intra-bar hits are captured.
- If both SL and TP are hit within the same bar, SL wins (conservative).
- Signal-reversal exits follow the live bot logic: opposite direction + strength >= 0.5.
- No same-bar re-entry: if a position closes, no new entry that bar.
- Single position at a time (max_concurrent_trades=1 for clean analysis).
- Costs: slippage + commission applied symmetrically on entry and exit.
- win-rate-based strategy switching is disabled (no DB); always uses regime default.
- 1m precision mode: when df_1m is passed to run(), each primary bar's exit check
  iterates the corresponding 1m sub-bars using numpy.searchsorted for O(log n)
  slicing.  Trailing-stop ratcheting happens at 1m resolution — no same-bar ambiguity.
- Coarse mode (no df_1m): exits checked at primary bar resolution; trailing stop
  ratcheted using bar high/low; same-bar SL+TP resolved conservatively (SL wins).
"""

import logging
from dataclasses import dataclass

import pandas as pd

from bot.bias.filter import BiasFilter, BiasFilterConfig
from bot.config_presets import get_regime_config, get_strategy_configs
from bot.constants import StrategyName
from bot.metrics import (
    max_consecutive_losses,
    max_drawdown,
    profit_factor,
    sharpe_ratio,
)
from bot.regime.detector import MarketRegime, RegimeDetector
from bot.strategy.base import Signal
from bot.strategy.breakout import BreakoutConfig, BreakoutStrategy
from bot.strategy.ema_crossover import EMACrossoverConfig, EMACrossoverStrategy
from bot.strategy.mean_reversion import MeanReversionConfig, MeanReversionStrategy
from bot.strategy.signal_factory import hold_signal

logger = logging.getLogger(__name__)

# Same mapping as the live orchestrator
_REGIME_STRATEGY_MAP: dict[MarketRegime, StrategyName] = {
    MarketRegime.TRENDING: StrategyName.EMA_CROSSOVER,
    MarketRegime.RANGING:  StrategyName.MEAN_REVERSION,
    MarketRegime.VOLATILE: StrategyName.BREAKOUT,
}

# Exit reason constants
EXIT_STOP_LOSS       = "STOP_LOSS"
EXIT_TAKE_PROFIT     = "TAKE_PROFIT"
EXIT_SIGNAL_REVERSAL = "SIGNAL_REVERSAL"
EXIT_END_OF_PERIOD   = "END_OF_PERIOD"
EXIT_LIQUIDATED      = "LIQUIDATED"

_OHLCV_COLS = ["open", "high", "low", "close", "volume"]


@dataclass
class BacktestConfig:
    initial_capital:   float = 10_000.0
    risk_per_trade:    float = 0.01
    timeframe:         str   = "1h"
    cost_per_side_pct: float = 0.0015
    min_signal_strength: float = 0.5
    min_4h_bars:       int   = 22
    reversal_strength_threshold: float = 0.75
    reversal_only_if_loss: bool = True
    min_hold_bars:     int   = 4
    post_close_cooldown_bars: int = 3
    hold_in_volatile:  bool  = True
    hold_in_ranging:   bool  = False
    disable_reversal_exits: bool = True
    force_strategy: str | None = None
    # EMA strategy TP/SL multipliers (searchable by optimizer)
    ema_stop_mult:     float = 1.5
    ema_tp_mult:       float = 3.5   # validated optimal: PF 1.187 vs 1.132 at 5.0×
    long_only:         bool  = False  # when True: EMA strategy ignores SELL signals
    # Trailing stop simulation (approximated at bar resolution)
    simulate_trailing:        bool  = True
    trail_atr_mult:           float = 1.5
    trail_activation_mult:    float = 2.0   # was 1.0 — activate only after 2×ATR profit
    # Leverage simulation (1.0 = spot, no change to existing behaviour)
    leverage:              float = 1.0
    funding_rate_per_8h:   float = 0.0001   # ~0.01% per 8h — typical BTC perp
    # Weekly momentum filter (defaults leave behaviour unchanged)
    momentum_filter_enabled: bool  = False
    momentum_sma_period:     int   = 20
    momentum_neutral_band:   float = 0.05


@dataclass
class BacktestResult:
    trades:          list[dict]
    equity_curve:    list[dict]
    initial_capital: float
    final_capital:   float
    timeframe:       str
    symbol:          str
    start_date:      str
    end_date:        str
    total_bars:      int


class BacktestEngine:
    """Simulates the complete live-bot pipeline (regime → strategy → bias → risk)
    bar by bar over a historical OHLCV DataFrame."""

    def __init__(self, config: BacktestConfig = BacktestConfig()) -> None:
        self.config = config
        tf = config.timeframe

        regime_cfg    = get_regime_config(tf)
        strategy_cfgs = get_strategy_configs(tf)

        self._regime_detector = RegimeDetector(config=regime_cfg)
        ema_cfg = dict(strategy_cfgs[StrategyName.EMA_CROSSOVER])
        ema_cfg["stop_atr_mult"] = config.ema_stop_mult
        ema_cfg["tp_atr_mult"]   = config.ema_tp_mult
        if config.long_only:
            ema_cfg["long_only"] = True

        self._strategies: dict[StrategyName, object] = {
            StrategyName.EMA_CROSSOVER: EMACrossoverStrategy(
                EMACrossoverConfig(**ema_cfg)
            ),
            StrategyName.MEAN_REVERSION: MeanReversionStrategy(
                MeanReversionConfig(**strategy_cfgs[StrategyName.MEAN_REVERSION])
            ),
            StrategyName.BREAKOUT: BreakoutStrategy(
                BreakoutConfig(**strategy_cfgs[StrategyName.BREAKOUT])
            ),
        }
        # BiasFilter is disabled when no 4h data is expected (e.g. 4h primary timeframe).
        # It will also auto-disable at runtime if run() receives df_4h=None.
        self._bias_filter = BiasFilter(BiasFilterConfig())

    # ── Private helpers ───────────────────────────────────────────────────────

    def _min_lookback(self) -> int:
        """Minimum bars needed before the regime detector can produce a valid result."""
        cfg = self._regime_detector.config
        return max(
            cfg.atr_period + cfg.atr_volatile_lookback,
            cfg.adx_period * 2,
            cfg.hurst_lookback,
        )

    def _get_4h_window(
        self,
        df_4h: pd.DataFrame | None,
        current_time: pd.Timestamp,
    ) -> pd.DataFrame | None:
        """Return the 4h OHLCV bars available at *current_time* (no lookahead)."""
        if df_4h is None:
            return None
        mask     = df_4h["open_time"] <= current_time
        filtered = df_4h[mask]
        if len(filtered) < self.config.min_4h_bars:
            return None
        # BiasFilter needs only 'close'; drop open_time before passing.
        # 250 bars allows macro filters (EMA200) without significant memory cost.
        return (
            filtered[_OHLCV_COLS]
            .tail(250)
            .reset_index(drop=True)
        )

    def _get_weekly_window(
        self,
        df_weekly: pd.DataFrame | None,
        current_time: pd.Timestamp,
    ) -> pd.DataFrame | None:
        """Return weekly bars completed before current_time (no lookahead)."""
        if df_weekly is None:
            return None
        mask     = df_weekly["open_time"] <= current_time
        filtered = df_weekly[mask]
        if len(filtered) < self.config.momentum_sma_period:
            return None
        return filtered[["open_time", "close"]].tail(self.config.momentum_sma_period + 5).reset_index(drop=True)

    def _get_momentum_state(self, weekly_window: pd.DataFrame | None) -> str:
        """Return 'BULLISH', 'BEARISH', or 'NEUTRAL' based on weekly SMA.

        BULLISH  → price > SMA × (1 + neutral_band)  → full risk
        BEARISH  → price < SMA × (1 − neutral_band)  → block entry
        NEUTRAL  → within the band                   → half risk
        Returns 'BULLISH' when filter is disabled or data is insufficient.
        """
        if not self.config.momentum_filter_enabled or weekly_window is None:
            return "BULLISH"
        closes = weekly_window["close"].to_numpy()
        sma    = closes[-self.config.momentum_sma_period:].mean()
        price  = float(closes[-1])
        band   = self.config.momentum_neutral_band
        if price > sma * (1.0 + band):
            return "BULLISH"
        if price < sma * (1.0 - band):
            return "BEARISH"
        return "NEUTRAL"

    def _generate_signal(
        self,
        window: pd.DataFrame,
        window_4h: pd.DataFrame | None,
    ) -> tuple[MarketRegime, Signal]:
        """Regime detection → strategy signal → bias filter gate."""
        regime        = self._regime_detector.detect(window)
        if self.config.force_strategy:
            strategy_name = StrategyName(self.config.force_strategy)
        else:
            strategy_name = _REGIME_STRATEGY_MAP[regime]
        strategy      = self._strategies[strategy_name]
        signal: Signal = strategy.generate_signal(window)

        bias = self._bias_filter.get_bias(window_4h)
        if not self._bias_filter.allows_signal(signal, bias):
            logger.debug(
                "Backtest: BiasFilter blocked %s signal (bias=%s)",
                signal.action, bias.value,
            )
            signal = hold_signal(atr=signal.atr)

        return regime, signal

    def _update_trailing(self, trade: dict, bar: pd.Series) -> None:
        """Ratchet the trailing stop using bar high/low (bar-resolution approximation)."""
        if not self.config.simulate_trailing:
            return
        atr   = trade.get("atr") or 0.0
        if atr <= 0:
            return

        trail_dist = self.config.trail_atr_mult * atr
        activation = self.config.trail_activation_mult * atr
        entry      = trade["entry_price"]
        side       = trade["side"]

        if side == "BUY":
            # Update peak using bar high
            trade["peak_price"] = max(trade.get("peak_price", entry), float(bar["high"]))
            if trade["peak_price"] >= entry + activation:
                new_trail = trade["peak_price"] - trail_dist
                if trade.get("trailing_sl") is None or new_trail > trade["trailing_sl"]:
                    trade["trailing_sl"] = new_trail
        else:
            # Update trough using bar low
            trade["peak_price"] = min(trade.get("peak_price", entry), float(bar["low"]))
            if trade["peak_price"] <= entry - activation:
                new_trail = trade["peak_price"] + trail_dist
                if trade.get("trailing_sl") is None or new_trail < trade["trailing_sl"]:
                    trade["trailing_sl"] = new_trail

    def _check_exit(
        self,
        trade: dict,
        bar: pd.Series,
    ) -> tuple[str, float] | None:
        """Return (exit_reason, raw_exit_price) if SL, trailing SL, or TP was hit this bar.

        Uses the bar's high and low to detect intra-bar hits.
        If both SL and TP are breached in the same bar, SL wins (conservative).
        Trailing stop is ratcheted first, then checked.
        """
        # Ratchet trailing stop before checking exits
        self._update_trailing(trade, bar)

        high = float(bar["high"])
        low  = float(bar["low"])
        sl   = trade["stop_loss"]
        tp   = trade["take_profit"]
        tsl  = trade.get("trailing_sl")

        if trade["side"] == "BUY":
            # Trailing SL takes priority over static SL
            active_sl = max(sl, tsl) if tsl is not None else sl
            if low <= active_sl:
                reason = "TRAILING_STOP" if tsl is not None and active_sl == tsl else EXIT_STOP_LOSS
                return reason, active_sl
            if high >= tp:
                return EXIT_TAKE_PROFIT, tp
        else:  # SELL
            active_sl = min(sl, tsl) if tsl is not None else sl
            if high >= active_sl:
                reason = "TRAILING_STOP" if tsl is not None and active_sl == tsl else EXIT_STOP_LOSS
                return reason, active_sl
            if low <= tp:
                return EXIT_TAKE_PROFIT, tp

        return None

    def _check_exit_precise(
        self, trade: dict, m1_slice: pd.DataFrame
    ) -> tuple[str, float] | None:
        """Check exit using 1m sub-bars — exact timing, correct SL/TP sequence.

        For each 1m bar we first ratchet the trailing stop (if enabled), then
        check SL/TP.  This eliminates the same-bar ambiguity of the coarse engine
        and gives ~1-minute precision on exit timing.
        """
        for _, m1_bar in m1_slice.iterrows():
            self._update_trailing(trade, m1_bar)
            result = self._check_exit(trade, m1_bar)
            if result is not None:
                return result
        return None

    def _apply_entry_cost(self, side: str, raw_price: float) -> float:
        """Adjust entry price for slippage + commission."""
        c = self.config.cost_per_side_pct
        return raw_price * (1 + c) if side == "BUY" else raw_price * (1 - c)

    def _apply_exit_cost(self, side: str, raw_price: float) -> float:
        """Adjust exit price for slippage + commission (mirror of entry cost)."""
        c = self.config.cost_per_side_pct
        return raw_price * (1 - c) if side == "BUY" else raw_price * (1 + c)

    def _compute_pnl(self, trade: dict, net_exit_price: float) -> float:
        entry = trade["entry_price"]
        qty   = trade["quantity"]
        if trade["side"] == "BUY":
            return (net_exit_price - entry) * qty
        return (entry - net_exit_price) * qty

    def _compute_quantity(
        self, capital: float, net_entry: float, stop_loss: float
    ) -> float:
        """Risk-based position sizing: risk_per_trade % of capital per trade."""
        risk_amount  = capital * self.config.risk_per_trade
        risk_per_unit = abs(net_entry - stop_loss)
        if risk_per_unit <= 0:
            return 0.0
        return round(risk_amount / risk_per_unit, 5)

    def _compute_quantity_with_risk(
        self, capital: float, net_entry: float, stop_loss: float, risk_per_trade: float
    ) -> float:
        """Risk-based sizing with an explicit risk_per_trade override."""
        risk_amount   = capital * risk_per_trade
        risk_per_unit = abs(net_entry - stop_loss)
        if risk_per_unit <= 0:
            return 0.0
        return round(risk_amount / risk_per_unit, 5)

    def _check_liquidation(self, trade: dict, bar: pd.Series) -> tuple[str, float] | None:
        """Return (EXIT_LIQUIDATED, liq_price) if the bar breaches the liquidation price.

        Liquidation price = entry * (1 - 0.9/leverage) for BUY
                          = entry * (1 + 0.9/leverage) for SELL
        The 0.9 factor accounts for Binance's maintenance margin buffer.
        Returns None when leverage <= 1.0 (spot — no liquidation possible).
        """
        if self.config.leverage <= 1.0:
            return None
        entry = trade["entry_price"]
        lev   = self.config.leverage
        if trade["side"] == "BUY":
            liq_price = entry * (1.0 - 0.9 / lev)
            if float(bar["low"]) <= liq_price:
                return EXIT_LIQUIDATED, liq_price
        else:
            liq_price = entry * (1.0 + 0.9 / lev)
            if float(bar["high"]) >= liq_price:
                return EXIT_LIQUIDATED, liq_price
        return None

    def _apply_leverage(self, raw_pnl: float, trade: dict, bars_held: int) -> float:
        """Scale raw P&L by leverage and subtract funding cost.

        When leverage == 1.0 returns raw_pnl unchanged.
        Funding cost = funding_rate_per_8h * bars_held * bar_hours / 8 * notional.
        """
        if self.config.leverage <= 1.0:
            return raw_pnl
        tf_h         = self._timeframe_hours(self.config.timeframe)
        notional     = trade["entry_price"] * trade["quantity"]
        funding_cost = self.config.funding_rate_per_8h * bars_held * tf_h / 8.0 * notional
        return raw_pnl * self.config.leverage - funding_cost

    # ── Main simulation ───────────────────────────────────────────────────────

    @staticmethod
    def _normalize_timestamps(df: pd.DataFrame) -> pd.DataFrame:
        """Ensure open_time is UTC-aware pd.Timestamp regardless of source format.

        Handles int64 (ms epoch), naive datetime64, and tz-aware inputs uniformly.
        Returns a copy so the original is not mutated.
        """
        df = df.copy()
        df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
        return df

    _REQUIRED_COLS: frozenset = frozenset({"open_time"} | set(_OHLCV_COLS))

    @staticmethod
    def _validate_inputs(df: pd.DataFrame, config: "BacktestConfig") -> None:
        """Raise ValueError on any data or config integrity violation."""
        # Config
        if config.initial_capital <= 0:
            raise ValueError(
                f"initial_capital must be > 0, got {config.initial_capital}"
            )
        if not (0 < config.risk_per_trade <= 1):
            raise ValueError(
                f"risk_per_trade must be in (0, 1], got {config.risk_per_trade}"
            )
        if config.cost_per_side_pct < 0:
            raise ValueError(
                f"cost_per_side_pct must be >= 0, got {config.cost_per_side_pct}"
            )
        # DataFrame
        missing = BacktestEngine._REQUIRED_COLS - set(df.columns)
        if missing:
            raise ValueError(f"df missing required columns: {missing}")
        if not df["open_time"].is_monotonic_increasing:
            raise ValueError(
                "df timestamps are not sorted ascending — sort by open_time before passing"
            )
        if (df["high"] < df["low"]).any():
            raise ValueError(
                "df contains bars where high < low — check data integrity"
            )

    def run(
        self,
        df: pd.DataFrame,
        df_4h: pd.DataFrame | None = None,
        symbol: str = "BTCUSDT",
        df_1m: pd.DataFrame | None = None,
        df_weekly: pd.DataFrame | None = None,
    ) -> BacktestResult:
        """Simulate the full bot pipeline bar by bar.

        Args:
            df:        OHLCV DataFrame with columns open_time, open, high, low,
                       close, volume.  Must be sorted ascending.
            df_4h:     Optional 4h DataFrame in the same format for BiasFilter.
            symbol:    Symbol name for reporting only.
            df_weekly: Optional weekly DataFrame for momentum filter.

        Returns:
            BacktestResult with all trades and equity curve.
        """
        self._validate_inputs(df, self.config)

        # Normalise timestamps to UTC-aware pd.Timestamp regardless of how the
        # caller obtained the DataFrames (int ms, naive datetime64, tz-aware, etc.)
        df = self._normalize_timestamps(df)
        if df_4h is not None:
            df_4h = self._normalize_timestamps(df_4h)
        if df_weekly is not None:
            df_weekly = self._normalize_timestamps(df_weekly)
        if df_1m is not None:
            df_1m = self._normalize_timestamps(df_1m)

        min_lb = self._min_lookback()
        if len(df) <= min_lb:
            raise ValueError(
                f"Insufficient data: {len(df)} bars, need > {min_lb} "
                f"(timeframe={self.config.timeframe})"
            )

        # If no 4h data provided, disable BiasFilter so it doesn't block all signals.
        # BiasFilter returns NEUTRAL (blocks everything) when df_4h is None.
        if df_4h is None:
            self._bias_filter = BiasFilter(BiasFilterConfig(enabled=False))

        # Pre-sort 1m DataFrame for O(log n) bar slicing via pandas searchsorted.
        # We keep df_1m as a DataFrame (not a numpy array) so that pandas handles
        # the UTC-aware timestamp comparisons correctly — avoiding the int vs Timestamp
        # error that np.searchsorted produces with DatetimeArray on pandas 2.x.
        has_1m = df_1m is not None and not df_1m.empty
        if has_1m:
            df_1m = df_1m.sort_values("open_time").reset_index(drop=True)
            logger.info(
                "Engine: 1m precision mode active — %d 1m bars loaded", len(df_1m)
            )

        capital     = self.config.initial_capital
        trades:     list[dict] = []
        open_trade: dict | None = None
        equity_curve: list[dict] = [{"bar": 0, "time": str(df.iloc[0]["open_time"]), "balance": capital}]
        cooldown_bars: int = 0   # bars remaining before next entry is allowed

        for i in range(min_lb, len(df)):
            bar          = df.iloc[i]
            current_time = bar["open_time"]

            # Slice for indicators — no open_time column, integer index
            window = df.iloc[: i + 1][_OHLCV_COLS].reset_index(drop=True)

            # 4h context — aligned to current_time (no lookahead)
            window_4h = self._get_4h_window(df_4h, current_time)

            # Weekly momentum state — controls entry gating and risk scaling
            weekly_window  = self._get_weekly_window(df_weekly, current_time)
            momentum_state = self._get_momentum_state(weekly_window)

            # Generate signal (regime + strategy + bias filter)
            regime, signal = self._generate_signal(window, window_4h)
            strategy_name  = _REGIME_STRATEGY_MAP[regime]

            closed_this_bar = False

            # ── 1. Check exits on open position ───────────────────────────────
            if open_trade is not None:
                # Liquidation check — only triggers when leverage > 1.0
                liq_info = self._check_liquidation(open_trade, bar)
                if liq_info is not None:
                    reason, liq_price   = liq_info
                    margin              = open_trade["entry_price"] * open_trade["quantity"] / self.config.leverage
                    notional            = open_trade["entry_price"] * open_trade["quantity"]
                    pnl                 = -margin
                    capital            += pnl

                    open_trade["exit_price"]  = liq_price
                    open_trade["exit_reason"] = EXIT_LIQUIDATED
                    open_trade["exit_bar"]    = i
                    open_trade["exit_time"]   = current_time
                    open_trade["pnl"]         = pnl
                    open_trade["pnl_pct"]     = pnl / notional if notional > 0 else -1.0

                    trades.append(open_trade)
                    open_trade      = None
                    closed_this_bar = True
                    cooldown_bars   = self.config.post_close_cooldown_bars
                    equity_curve.append({"bar": i, "time": str(current_time), "balance": capital})
                else:
                    if has_1m:
                        next_time = (
                            df.iloc[i + 1]["open_time"]
                            if i + 1 < len(df)
                            else pd.Timestamp.max.tz_localize("UTC")
                        )
                        # Use pandas Series.searchsorted — handles UTC-aware timestamps
                        # correctly without the int vs Timestamp errors of np.searchsorted.
                        lo = int(df_1m["open_time"].searchsorted(current_time, side="left"))
                        hi = int(df_1m["open_time"].searchsorted(next_time,   side="left"))
                        m1_slice = df_1m.iloc[lo:hi]
                        exit_info = (
                            self._check_exit_precise(open_trade, m1_slice)
                            if len(m1_slice) > 0
                            else self._check_exit(open_trade, bar)
                        )
                    else:
                        exit_info = self._check_exit(open_trade, bar)

                    # Signal reversal — skipped when disable_reversal_exits=True
                    if exit_info is None and not self.config.disable_reversal_exits:
                        bars_held = i - open_trade["entry_bar"]
                        current_price = float(bar["close"])
                        is_in_loss = (
                            (open_trade["side"] == "BUY"  and current_price < open_trade["entry_price"]) or
                            (open_trade["side"] == "SELL" and current_price > open_trade["entry_price"])
                        )
                        reversal_allowed = (
                            bars_held >= self.config.min_hold_bars
                            and (not self.config.reversal_only_if_loss or is_in_loss)
                        )
                        opposite = (
                            (
                                (open_trade["side"] == "BUY"  and signal.action == "SELL") or
                                (open_trade["side"] == "SELL" and signal.action == "BUY")
                            )
                            and signal.strength >= self.config.reversal_strength_threshold
                            and reversal_allowed
                        )
                        if opposite:
                            exit_info = (EXIT_SIGNAL_REVERSAL, float(bar["close"]))

                    if exit_info is not None:
                        reason, raw_exit       = exit_info
                        net_exit               = self._apply_exit_cost(open_trade["side"], raw_exit)
                        bars_held              = i - open_trade["entry_bar"]
                        raw_pnl                = self._compute_pnl(open_trade, net_exit)
                        pnl                    = self._apply_leverage(raw_pnl, open_trade, bars_held)
                        notional               = open_trade["entry_price"] * open_trade["quantity"]
                        capital               += pnl

                        open_trade["exit_price"]  = net_exit
                        open_trade["exit_reason"] = reason
                        open_trade["exit_bar"]    = i
                        open_trade["exit_time"]   = current_time
                        open_trade["pnl"]         = pnl
                        open_trade["pnl_pct"]     = pnl / notional if notional > 0 else 0.0

                        trades.append(open_trade)
                        open_trade      = None
                        closed_this_bar = True
                        cooldown_bars   = self.config.post_close_cooldown_bars
                        equity_curve.append({"bar": i, "time": str(current_time), "balance": capital})

            # Tick down cooldown counter each bar
            if cooldown_bars > 0:
                cooldown_bars -= 1

            # ── 2. Entry check ────────────────────────────────────────────────
            volatile_skip = self.config.hold_in_volatile and regime == MarketRegime.VOLATILE
            ranging_skip  = self.config.hold_in_ranging  and regime == MarketRegime.RANGING
            if (
                not closed_this_bar
                and open_trade is None
                and cooldown_bars == 0
                and not volatile_skip
                and not ranging_skip
                and momentum_state != "BEARISH"
                and signal.action != "HOLD"
                and signal.strength >= self.config.min_signal_strength
                and signal.stop_loss is not None
                and signal.stop_loss > 0
            ):
                raw_entry     = float(bar["close"])
                net_entry     = self._apply_entry_cost(signal.action, raw_entry)
                effective_risk = (
                    self.config.risk_per_trade * 0.5
                    if momentum_state == "NEUTRAL"
                    else self.config.risk_per_trade
                )
                quantity = self._compute_quantity_with_risk(
                    capital, net_entry, signal.stop_loss, effective_risk
                )

                if quantity > 0:
                    open_trade = {
                        "entry_bar":   i,
                        "entry_time":  current_time,
                        "side":        signal.action,
                        "entry_price": net_entry,
                        "stop_loss":   signal.stop_loss,
                        "take_profit": signal.take_profit,
                        "quantity":    quantity,
                        "atr":         signal.atr,
                        "strategy":    strategy_name.value,
                        "regime":      regime.value,
                        # trailing stop tracking
                        "peak_price":  net_entry,
                        "trailing_sl": None,
                        # filled on close:
                        "exit_bar":    None,
                        "exit_time":   None,
                        "exit_price":  None,
                        "exit_reason": None,
                        "pnl":         None,
                        "pnl_pct":     None,
                    }
                    logger.debug(
                        "Backtest: opened %s @ %.2f  SL=%.2f  TP=%.2f  qty=%.5f",
                        signal.action, net_entry, signal.stop_loss,
                        signal.take_profit, quantity,
                    )

        # ── Close any still-open position at last bar ─────────────────────────
        if open_trade is not None:
            raw_exit  = float(df.iloc[-1]["close"])
            net_exit  = self._apply_exit_cost(open_trade["side"], raw_exit)
            bars_held = len(df) - 1 - open_trade["entry_bar"]
            raw_pnl   = self._compute_pnl(open_trade, net_exit)
            pnl       = self._apply_leverage(raw_pnl, open_trade, bars_held)
            notional  = open_trade["entry_price"] * open_trade["quantity"]
            capital  += pnl

            open_trade["exit_price"]  = net_exit
            open_trade["exit_reason"] = EXIT_END_OF_PERIOD
            open_trade["exit_bar"]    = len(df) - 1
            open_trade["exit_time"]   = df.iloc[-1]["open_time"]
            open_trade["pnl"]         = pnl
            open_trade["pnl_pct"]     = pnl / notional if notional > 0 else 0.0
            trades.append(open_trade)
            equity_curve.append({"bar": len(df) - 1, "time": str(df.iloc[-1]["open_time"]), "balance": capital})

        start_date = df.iloc[min_lb]["open_time"].strftime("%Y-%m-%d")
        end_date   = df.iloc[-1]["open_time"].strftime("%Y-%m-%d")

        return BacktestResult(
            trades          = trades,
            equity_curve    = equity_curve,
            initial_capital = self.config.initial_capital,
            final_capital   = capital,
            timeframe       = self.config.timeframe,
            symbol          = symbol,
            start_date      = start_date,
            end_date        = end_date,
            total_bars      = len(df) - min_lb,
        )

    # ── Metrics summary (convenience) ─────────────────────────────────────────

    @staticmethod
    def _timeframe_hours(tf: str) -> float:
        if tf.endswith("m"):
            return int(tf[:-1]) / 60.0
        if tf.endswith("h"):
            return float(tf[:-1])
        if tf.endswith("d"):
            return float(tf[:-1]) * 24.0
        return 1.0

    def summary(self, result: BacktestResult) -> dict:
        """Compute all metrics from a BacktestResult and return them as a dict."""
        closed = [t for t in result.trades if t["exit_reason"] != EXIT_END_OF_PERIOD]
        wins   = [t for t in closed if (t["pnl"] or 0) > 0]

        tf_h  = self._timeframe_hours(result.timeframe)
        total_pnl     = result.final_capital - result.initial_capital
        total_pnl_pct = total_pnl / result.initial_capital * 100

        return {
            "total_trades":          len(closed),
            "open_at_period_end":    len(result.trades) - len(closed),
            "win_rate_pct":          len(wins) / len(closed) * 100 if closed else 0.0,
            "total_pnl":             total_pnl,
            "total_pnl_pct":         total_pnl_pct,
            "sharpe_ratio":          sharpe_ratio(result.equity_curve, timeframe_hours=tf_h),
            "max_drawdown_pct":      max_drawdown(result.equity_curve) * 100,
            "profit_factor":         profit_factor(closed),
            "max_loss_streak":       max_consecutive_losses(closed),
            "best_trade_pnl":        max((t["pnl"] for t in closed), default=0.0),
            "worst_trade_pnl":       min((t["pnl"] for t in closed), default=0.0),
        }
