# Strategy review — 2026-08-15

Applied `tradermonty/claude-trading-skills` to this repo. Two of its 71 skills
were relevant (`backtest-expert`, `crypto-regime-analyzer`); triage and rejection
reasons are in `docs/trading_skills.md`. This is what applying them found.

**Headline: the strategy is healthy and scores Deploy (87/100). The two things
that were actually broken were infrastructure and measurement, not the edge.**

---

## 1. Live incident — the bot was dead for 2 days 8 hours

Found while checking whether the live losing streak was real.

On **2026-08-13 02:58 UTC** a `ConnectionResetError` during a Binance SSL
handshake escaped `position_manager`, propagated out of
`schedule.run_pending()`, and killed `main()`'s loop. The container stayed `Up`
with `RestartCount: 0` because `ThreadedWebsocketManager` runs **non-daemon**
threads — the process never exited, so `restart: unless-stopped` never fired.

Nothing ran for 2d 8h. No entries, and — the part that matters — **no
`position_manager`, which is the only thing watching stops on open positions**.
The open SOLUSDT trade (#20, entry 76.42, SL 75.0893) had its stop breached on
2026-08-14 14:00 (low 75.06, session low 74.69) and no exit was executed.

Fixed and deployed the same day (`trading-bot:local-20260815-schedguard`):
per-job `guarded()` wrapper + `run_scheduler_tick()` + `try/finally` cleanup.
Full write-up, diagnosis recipe and rationale: **gotcha #44**. Regression guard:
`tests/test_scheduler_resilience.py` (5 tests).

> The cheapest liveness probe is `SELECT MAX(timestamp) FROM equity` — it gets a
> row per hourly cycle. "Container Up" proves nothing.

---

## 2. Measurement bug — CLAUDE.md's risk table describes a strategy that isn't running

`scripts/validate_live_risk_2026.py` built its `BacktestConfig` by hand. That
inherits the **research** dataclass defaults, which differ from live in **10
fields** — including all four entry-quality filters (off vs on), `bias_strict`
(off vs on), Kelly (on vs off) and `ema_tp_mult` (4.5 vs the seeded 5.0).

Its output became the "Risk × DD re-run (2026-07-30)" table in CLAUDE.md.

Measured over the same 3y window, the difference is not cosmetic:

| 3y, BTC+ETH+SOL, 1.5% risk | Annual | Max DD | PF | Calmar | Trades |
|---|---|---|---|---|---|
| **Live-parity config** | +12.5% | **10.4%** | **1.36** | **1.20** | 253 |
| The config that table used | +10.6% | 21.7% | 1.14 | 0.49 | 326 |

**The old table understated the live strategy — it doubled the drawdown and
more than halved the Calmar.** The entry-quality filters and `bias_strict` are
doing real work: 22% fewer trades, and the ones skipped are the bad ones.

Written up as **gotcha #43**, with the rule: any number quoted as evidence about
live must come from `build_backtest_config`. Also documented in
`docs/backtest_vs_live.md` §4.1. `scripts/stress_test_2026.py` is the reference
implementation.

---

## 3. Strategy stress test — `backtest-expert` methodology

The cache only held data from 2023-05, so the strategy had **never been tested
through the 2021 top or the 2022 bear**. `scripts/fetch_long_history.py` extends
it to 2017-08 (19,696 4h bars per symbol). All runs below use the live config.

### 3.1 Baseline

| Window | Annual | Max DD | PF | Calmar | Trades | Win% |
|---|---|---|---|---|---|---|
| BTC+ETH, 2017-08→now (9y) | +15.9% | 12.2% | 1.48 | 1.31 | 477 | 34.6% |
| BTC+ETH+SOL, 2020-08→now (6y) | +12.8% | 10.4% | 1.38 | 1.23 | 498 | 32.3% |

477 trades over 9 years clears the skill's "high confidence" bar (200+) by a
wide margin. Average win **13.0%** of notional vs average loss **4.0%** — a
3.25:1 ratio, consistent with TP 5×ATR / SL 1.5×ATR. Expectancy **+1.95%/trade**.

### 3.2 Parameter sensitivity — plateaus, not peaks

Calmar over the stop × TP grid (6y, 3 symbols):

| stop \ TP | 4.0 | 4.5 | 5.0 | 5.5 | 6.0 |
|---|---|---|---|---|---|
| 0.750 | 0.31 | 0.42 | 0.62 | 0.84 | 1.02 |
| 1.125 | 0.71 | 1.00 | 1.16 | 1.20 | 1.37 |
| **1.500** | 0.80 | 1.19 | **1.23** | 1.45 | 1.53 |
| 1.875 | 0.87 | 0.93 | 1.18 | 1.26 | 1.36 |
| 2.250 | 0.80 | 0.90 | 1.08 | 1.22 | 1.29 |

Two different readings, and they point opposite ways:

- **Stop = 1.5 is a genuine plateau.** Down the TP=5.0 column: 0.62 → 1.16 →
  **1.23** → 1.18 → 1.08. Cleanly centred, no cliff on either side. This is what
  a non-curve-fit parameter looks like, and it holds across every TP column.
- **TP = 5.0 is not a peak — it is a point on a monotonic climb.** Along the
  stop=1.5 row Calmar rises all the way to the grid edge (1.53 at TP=6.0), and
  the same is true in every other row. The optimum is **outside the tested
  range**.

That second point has a structural consequence: `bot/optimizer/walk_forward.py`
has `TP_GRID = [2.5 … 5.0]`, capped at exactly the current live value. **The
optimizer cannot discover the better region even in principle.** Both auto
optimizers are disabled (gotcha #33), so nothing is acting on this today.

**Not proposing a TP change here.** A monotonic rise to a boundary is exactly
what a plain grid search over-reads, higher TP also means fewer trades and
therefore less fee drag, and CLAUDE.md's rule is that sensitive parameters move
only behind a walk-forward. The correct next step is to *widen the grid* and run
`scripts/audit/run_walk_forward.py` over TP ∈ [5.0 … 8.0], not to edit a config.

### 3.3 Execution friction

The skill wants 1.5–2× typical costs. Costs are per side; baseline 0.10%.

| Cost/side | Annual | Max DD | PF | Calmar |
|---|---|---|---|---|
| 0.10% (1×) | +12.8% | 10.4% | 1.38 | 1.23 |
| 0.15% (1.5×) | +11.1% | 10.9% | 1.33 | 1.02 |
| 0.20% (2×) | +9.6% | 11.5% | 1.28 | 0.84 |
| 0.30% (3×) | +6.7% | 12.7% | 1.19 | 0.53 |

Still profitable at **3× the assumed cost**. Passes comfortably — the edge is
not a fee-structure artifact.

### 3.4 Time robustness

| Year | Annual | Max DD | PF | Trades | Win% | Regime |
|---|---|---|---|---|---|---|
| 2018 | −4.4% | 11.3% | 0.74 | 27 | 18.5% | bear |
| 2019 | +26.2% | 7.3% | 2.08 | 46 | 41.3% | recovery |
| 2020 | +41.0% | 6.9% | 2.17 | 67 | 43.3% | covid crash + bull |
| 2021 | +19.4% | 7.1% | 1.44 | 109 | 32.1% | blow-off top |
| 2022 | −1.4% | 8.4% | 0.93 | 49 | 24.5% | bear |
| 2023 | +10.1% | 6.9% | 1.38 | 74 | 32.4% | recovery |
| 2024 | +16.9% | 7.6% | 1.53 | 86 | 33.7% | bull |
| 2025 | +8.1% | 5.9% | 1.33 | 67 | 31.3% | chop/decline |
| 2026 | −10.2% | 10.4% | 0.62 | 41 | 19.5% | bear |

**6 of 9 years positive** — passes the skill's majority requirement. Every losing
year is a bear market, and the losses are small relative to what the underlying
did. This is the textbook signature of a long-only trend follower, not a broken
edge. Max DD never exceeds 11.3% in any single year.

### 3.5 Hypothesis tested and REJECTED — the bear gates do not help

The strategy ships two entry gates that are **off** in live and that look like
the obvious fix for the current bear: `require_ema200_alignment` (block longs
below EMA200) and `min_entry_adx`. `scripts/test_bear_filters.py` judges them
across regimes rather than on the current bear alone — a gate that only rescues
the bear is regime-fitting.

Calmar by variant (higher is better; **bold = best in column**):

| Variant | full 6y | 2020-21 bull | 2022 bear | 2023-24 | 2025-26 chop |
|---|---|---|---|---|---|
| **baseline (both off) — live** | **1.23** | **3.50** | **−0.34** | 1.59 | **0.36** |
| EMA200 alignment ON | 1.07 | **3.50** | −0.52 | **1.60** | 0.33 |
| min entry ADX 20 | 0.83 | 3.38 | −0.40 | 1.00 | 0.11 |
| min entry ADX 25 | 0.63 | 3.05 | −0.60 | 1.51 | 0.04 |
| EMA200 + ADX 20 | 0.82 | 3.38 | −0.40 | 1.01 | 0.06 |

**Hypothesis rejected. The live setting (both off) is optimal or tied in every
window.** Two results worth keeping:

- **EMA200 alignment makes the 2022 bear *worse*, not better** (Calmar −0.34 →
  −0.52, win rate 23.5% → 18.8%). The intuition "don't buy below the 200 EMA" is
  wrong for this system: it does not remove many trades (34 → 32) but it delays
  re-entry on the recovery legs, which is where a long-only trend follower makes
  the money that pays for the bear. It is a *worse* filter precisely where it
  was supposed to help.
- **ADX gating is uniformly destructive**, and gets worse the tighter it is. It
  only touches continuation entries, so it removes the trades nearest the EMA —
  the cheapest ones — while leaving the extended entries untouched.

**This resolves an explicitly open item.** The Phase-2 acceptance audit
(`docs/audits/CC_2026-05-17_PHASE2_SUMMARY.md`, 2022-04→2026-05) reached:

- `C3_ADX25` / `C3_ADX30` / `C3_BOTH` → **REJECT** (0/10 and 1/9 windows)
- `C3_EMA200` → **INCONCLUSIVE** (p=0.29, 4 wins/3 losses) —
  *"EMA200 neutral. Keep defaults OFF pending longer test."*

The longer test is now done. Over 9 years and 5 regime windows EMA200 alignment
is **not** neutral: it is mildly negative overall (6y Calmar 1.23 → 1.07) and
clearly negative in the bear it was supposed to protect against. The May verdict
can be upgraded from INCONCLUSIVE to REJECT. ADX independently reproduces as
REJECT, which is a good sanity check on this harness.

**Action: none. Leave `ema_require_ema200=false` and `ema_min_entry_adx=0.0`.**
This is a confirmation that the deployed config is right, and it closes off the
most tempting "fix" for the current losing stretch.

### 3.6 Score

`evaluate_backtest.py` on the 9-year BTC+ETH baseline:

| Dimension | Score |
|---|---|
| Sample Size | 20/20 |
| Expectancy | 20/20 |
| Risk Management | 14/20 |
| Robustness | 13/20 |
| Execution Realism | 20/20 |
| **Total** | **87/100 — Deploy** |

One MEDIUM red flag: **9 tunable parameters** is curve-fitting surface. Worth
remembering before adding a tenth knob.

Full report: `docs/audits/backtest_eval_2026-08-15_135800.md`.

---

## 4. Is the live losing streak the edge failing?

Live record: **12 closed trades, 1 win, PF 0.22, −231.36 USDT** (−2.3% on the
10,000 baseline). Eleven stop-outs.

Three independent checks say this is environment, not breakage:

1. **`crypto-regime-analyzer` (2026-08-15): RISK_OFF, 37.4/100.** BTC in a bear
   stack (price < 50DMA < 200DMA, 200DMA falling), 49.5% off the 1y high, only
   35% of the top-20 positive over 30d, breadth 37%.
2. **The backtest agrees.** 2026 in the table above is −10.2% with a 19.5% win
   rate — the model reproduces the live experience on out-of-sample data.
3. **Sample size.** 12 trades is below the skill's 30-trade minimum for any
   conclusion. P(≤1 win in 12 | true rate 34%) = **4.9%** — low, but not
   evidence of a broken system once you account for the regime, and 4 of those
   12 trades were opened by the pre-gotcha-#41 code that traded forming candles.

**The live sample is also contaminated** and should not be used as a scorecard:
trades 8–15 predate the closed-candle fix, and the SOL trade spans the 2-day
outage. A clean live evaluation starts from 2026-08-15.

---

## 5. What was verified as correct

- **Gotcha #41 fix works in production.** Post-fix entries land on 4h
  boundaries (08:00, 04:00, 04:00); pre-fix ones were at 17:00, 22:00, 10:00.
- **Deployed image == working tree**, byte-identical on `main.py`.
- **Live runtime config matches the documented baseline**: risk 0.015,
  stop 1.5, TP 5.0, symbols BTC+ETH+SOL, long_only, Kelly off, breaker at 15%.
- **564 tests pass.**

## 6. Open items

1. **Nothing is committed.** Both fixes (gotcha #41, #44) run in production from
   a local image that CI has never built. A `docker system prune` that removed
   the image, or anyone reverting `docker-compose.yml`, silently reverts both.
   This is the highest-priority follow-up.
2. **Widen `TP_GRID` past 5.0** and re-run the walk-forward audit (§3.2).
3. **`_last_entry_bar` is in-memory.** A container restart clears it, so the bar
   in progress at restart can be evaluated twice. The host reboots Mondays 05:00.
   Low impact (an open position blocks re-entry) but not zero.
4. **No liveness alerting.** §1 went unnoticed for 2 days 8 hours. The daily 09:00
   heartbeat exists but its absence is not alerted on — a missing message is
   silence, and silence is indistinguishable from a healthy quiet bot.
