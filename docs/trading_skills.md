# External skills — `tradermonty/claude-trading-skills`

Evaluated 2026-08-15 against this repo. Source:
<https://github.com/tradermonty/claude-trading-skills> (MIT).

## Verdict: 2 of 71 skills apply

The upstream repo is a **discretionary, human-in-the-loop workflow toolkit for
US equities, ETFs and dividend stocks**. Its own README states it is "not
designed for fully automated trading, signal outsourcing, or short-term
scalping" — which is exactly what this repo is. Most skills are therefore
structurally inapplicable, not merely redundant.

Installed under `.claude/skills/`:

| Skill | Why it applies |
|---|---|
| `backtest-expert` | Pure methodology — no asset-class assumptions. Its stress-test workflow (parameter plateaus, friction multipliers, year-by-year robustness, sample-size floors) is directly runnable against `PortfolioBacktestEngine`, and its `scripts/evaluate_backtest.py` scores a strategy on 5 dimensions. Drove `scripts/stress_test_2026.py`. |
| `crypto-regime-analyzer` | The only crypto-native skill. Keyless (CoinGecko + Binance public endpoints), scores market regime 0–100 across 6 components. Gives an **independent** read on whether a losing stretch is the edge failing or the market being hostile — the exact question CLAUDE.md's buy & hold benchmark section exists to answer. |

## Rejected, with reasons

Rejecting these is the point of the exercise — installing all 71 would bury the
2 that matter.

| Skill(s) | Why not |
|---|---|
| `position-sizer`, `futures-position-sizer` | Size **shares** of stock from account equity. `bot/risk/manager.py` already does risk-based sizing with a spot capital cap (gotcha #23) plus Kelly (`bot/risk/kelly.py`). Adopting these would be a downgrade. |
| `drawdown-circuit-breaker` | Reads `trader-memory-core` thesis YAML and emits an advisory decision for a human. This repo has a persisted, per-symbol, automated breaker consuming trading equity (gotchas #4/#30/#31). |
| `signal-postmortem`, `trade-performance-coach`, `trader-memory-core` | Journaling/coaching for a human trader's process adherence and psychology. There is no human in this loop — the bot has a DB, a dashboard and Telegram `/report`. |
| `vcp-screener`, `canslim-screener`, `finviz-screener`, `pead-screener`, `earnings-*`, `ibd-*`, `stockbee-*`, `value-dividend-screener`, `dividend-growth-pullback-screener` | US single-stock screeners. Need FMP/FINVIZ Elite keys and depend on earnings, fundamentals and sector rotation. The universe here is 3 crypto pairs, fixed. |
| `market-breadth-analyzer`, `uptrend-analyzer`, `exposure-coach`, `sector-analyst`, `macro-regime-detector`, `market-top-detector`, `us-market-bubble-detector` | Equity-market breadth/macro. `crypto-regime-analyzer` is the crypto analog and is the one installed. |
| `edge-*` pipeline (9 skills), `skill-*` (designer/miner/reviewer/tester) | A strategy-generation pipeline producing draft YAML for human review, plus meta-skills for authoring skills. Orthogonal to maintaining one validated strategy. |
| `scenario-analyzer`, `market-news-analyst`, `theme-detector`, `cot-contrarian-detector`, `economic-calendar-fetcher`, `fxmacrodata-calendar` | News/narrative analysis for discretionary positioning. This bot is mechanical; `bot/risk/news_pause.py` already covers the only news concept it uses, and it is disabled. |
| `options-strategy-advisor`, `pair-trade-screener`, `parabolic-short-trade-planner`, `mt5-robot-tester` | Instruments and platforms this bot does not trade. |
| `data-quality-checker` | Validates prose documents (blogs, market reports) pre-publication. No such artifact here. |

## How they are used

Neither skill is wired into the running bot. Both are **analysis tools for
maintenance sessions** — the bot's runtime dependencies are unchanged.

```bash
# Independent read on market conditions before judging live results
python .claude/skills/crypto-regime-analyzer/scripts/crypto_regime_analyzer.py \
    --output-dir docs/audits/regime/

# Score a backtest on the 5-dimension framework
python .claude/skills/backtest-expert/scripts/evaluate_backtest.py \
    --total-trades 477 --win-rate 35 --avg-win-pct 8.6 --avg-loss-pct 2.4 \
    --max-drawdown-pct 12 --years-tested 9 --num-parameters 8 --slippage-tested \
    --output-dir docs/audits/
```

`crypto-regime-analyzer` hits CoinGecko's free tier and **will return HTTP 429**
if run repeatedly in a short window. Wait a minute and retry; it is not broken.

## Repo changes this evaluation produced

The skills were the lens, not the deliverable. What came out of applying them:

- `scripts/fetch_long_history.py` — kline cache extended 2023-05 → **2017-08**.
  `backtest-expert` requires 5y minimum and multi-regime coverage; the strategy
  had never been tested through the 2021 top or the 2022 bear.
- `scripts/stress_test_2026.py` — the skill's stress workflow run against the
  **live-parity** config (see `docs/audits/strategy_review_2026-08-15.md`).
- `scripts/test_bear_filters.py` — falsifiable test of the two shipped-but-off
  bear gates, judged across regimes rather than on the current bear alone.
