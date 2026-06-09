# Add SOLUSDT for Diversification + Capital-Allocation Parity Finding

**Date**: 2026-06-09
**Status**: Implemented
**Owner**: Jordi
**Author**: trading analysis session (acting as 15-yr discretionary trader)
**Test runner**: `venv/bin/pytest tests/ -q`

---

## 1. Context

User asked: the validated baseline is "apenas inamovible" (almost immovable) —
is there anything we can do to improve it? Implement with SDD if a real edge is
found. Constraint: do not touch the validated strategy params (TP/SL/risk/
timeframe/bias) without strong, sub-period-robust evidence.

Live production config (read from the container DB, 2026-06-09) — note it has
**diverged from the documented baseline**:

| key | live value | CLAUDE.md baseline |
|---|---|---|
| `risk_per_trade` | **0.025** | 0.015 |
| `kelly_enabled` | **false** (flat risk) | true |
| `symbols` | `BTCUSDT,ETHUSDT` | BTC+ETH |
| `ema_tp_mult` / `ema_stop_mult` | 5.0 / 1.5 | 5.0 / 1.5 |
| `bias_neutral_passthrough` | false (strict) | strict |

All backtests in this spec use the **live** config (2.5% flat, no Kelly).

## 2. What we tested (3y BTC/ETH 4h cache, full + sub-periods)

Research scripts under `scripts/research/improve_2026_06*.py`,
`parity_alloc_2026_06.py`, `robust_sol_2026_06.py`. Every additive lever was
measured against the live baseline:

- **TP/SL re-optimization** (TP 6/7, SL 1.25–2.0): all WORSE. TP=5.0 is already
  near-optimal; raising it cuts Calmar 1.73 → 0.64–0.92.
- **Partial-TP ladder**: no effect — `PortfolioBacktestEngine` does not implement
  it (only the single-symbol engine does). Latent parity gap, not an improvement.
- **Entry-quality gates** (`ema_min_entry_adx`, `ema_require_ema200`): ADX>15
  cuts the fat-tail breakout winners (Calmar collapses); EMA200 alignment is a
  no-op (bias_strict + momentum already gate to uptrends). Only `adx=15` is
  neutral-to-marginal (PF 1.52→1.54, same CAGR/DD). Not worth a baseline change.
- **Vol-regime filter** (block/reduce low-vol): all WORSE. Low vol *precedes* the
  breakouts this trend-follower lives on; blocking them removes the right tail
  (CAGR 39.6 → 19–25%).

**Conclusion**: the strategy is well-tuned. Every parameter tweak hurts. The only
structural free lunch left is **diversification**.

## 3. Key finding — capital-allocation parity gap (gotcha #40)

`PortfolioBacktestEngine` (`portfolio_engine.py:312`) sizes **each symbol off the
full shared pool** `capital`. Live (`main.run_cycle`, gotcha #22) sizes each
symbol off `balance = total/N`. So the dashboard/backtest **overstates live by
~N×** as the symbol count grows:

| BTC+ETH (3y) | CAGR | MaxDD | Calmar | PF |
|---|---|---|---|---|
| Engine as-is (dashboard) | 39.6% | 22.8% | 1.73 | 1.52 |
| **÷N (true live)** | **19.5%** | **12.1%** | **1.61** | **1.58** |

The celebrated "~40% config" (`project_strategy_levers_40pct.md`) is the inflated
engine number. **True live BTC+ETH is ~20% CAGR / 12% DD.** Documented as gotcha
#40; the *fix* (make the engine divide by N) is out of scope here — it would
change every dashboard number and touches tested code. Recorded for a follow-up.

## 4. Decision: add SOLUSDT as a third symbol

SOL is decorrelated enough (4h return corr to BTC 0.73, to ETH 0.72) to smooth
the curve, and — critically — its *standalone* Calmar (0.80) is **worse** than
BTC's (1.33). So this is **not return-chasing the best alt**; it is textbook
decorrelation. BNB was rejected (standalone Calmar −0.04, toxic for this system).

### Success criterion (predetermined): SOL must improve risk-adjusted return
under the **true live allocation (÷N)** in **both** sub-period halves, not just
the full window — i.e. lower DD and/or higher Calmar with no CAGR collapse.

### Result — criterion met in full

| Window (÷N, live alloc) | Set | CAGR | MaxDD | Calmar | Sharpe |
|---|---|---|---|---|---|
| Full 3y | BTC+ETH | 19.5 | 12.1 | 1.61 | 1.31 |
| Full 3y | **+SOL** | 18.7 | **9.4** | **1.99** | **1.34** |
| H1 2023-06→2024-12 | BTC+ETH | 32.2 | 12.1 | 2.66 | 1.79 |
| H1 | **+SOL** | 28.7 | **9.4** | **3.06** | 1.68 |
| H2 2024-12→2026-05 (weak/chop) | BTC+ETH | 7.3 | 7.6 | 0.97 | 0.66 |
| H2 | **+SOL** | **11.0** | **6.7** | **1.64** | **1.01** |

- Full 3y: DD −22% (12.1→9.4), Calmar +24% (1.61→1.99), CAGR ≈ flat.
- H2 (the hard regime where BTC+ETH stalled): SOL improves **everything** —
  +CAGR, −DD, Calmar +69%. Diversification that helps *most* when it's needed
  most is the robustness signal we required.

Improvement is robust to the parity gap — Calmar rises in both engines
(as-is 1.73→2.12, ÷N 1.61→1.99).

## 5. Implementation

1. `main._seed_optimized_defaults`: seed `symbols = "BTCUSDT,ETHUSDT,SOLUSDT"`
   so fresh DBs (incl. post-testnet-reset) start with the validated 3-symbol set.
   Seed is set-if-absent — never overwrites a user's manual `symbols`.
2. Live container DB: `db.set_symbols([...SOL])` to apply now (bot is on testnet,
   idle since the 2026-06-03 reset — zero-stakes rollout).
3. `CLAUDE.md`: update the Validated Baseline symbol row + add the ÷N parity
   caveat and real numbers.
4. `docs/gotchas.md`: add gotcha #40 (capital-allocation parity gap).

## 6. Non-goals / follow-ups

- **Fix the engine ÷N parity** so the dashboard stops overstating live (large,
  touches tested code — separate PR).
- **Capital-efficiency rework**: the live 1/N split leaves capital idle. A
  shared-pool allocator (deploy to whoever has a signal, cap total heat) could
  roughly double CAGR — but raises DD and is a sensitive change to live sizing.
  Needs its own spec + the user's risk-appetite decision.
- Re-evaluate `risk_per_trade=0.025` vs the documented 0.015 (live has drifted).
