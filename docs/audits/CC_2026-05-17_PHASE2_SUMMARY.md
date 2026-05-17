# Phase 2 Acceptance Audit — Summary

*Generated: 2026-05-17 11:56 UTC*

Champion: **C3_LIVE** | Date range: 2022-04-01 → 2026-05-01

## Results

| Challenger | Verdict | Calmar p | Cohen's d | Wins/Losses | Calmar (champ/chal) | Kill-Switch |
|------------|---------|----------|-----------|-------------|---------------------|-------------|
| C3_ADX25 | **REJECT** | 0.0001 | -2.1929 | 0/10 | 13.52 / 3.53 | no (685) |
| C3_ADX30 | **REJECT** | 0.0041 | -1.2063 | 1/9 | 13.52 / 8.03 | no (585) |
| C3_EMA200 | **INCONCLUSIVE** | 0.2940 | -0.3524 | 4/3 | 13.52 / 13.28 | no (912) |
| C3_BOTH | **REJECT** | 0.0001 | -2.2022 | 0/10 | 13.52 / 3.58 | no (685) |

## Acceptance Criteria (design D9)

- **ADOPT**: Calmar p < 0.05 AND Cohen's d > 0.5 AND challenger wins ≥ 60% of windows
- **REJECT**: challenger loses ≥ 60% of windows OR kill-switch triggered
- **INCONCLUSIVE**: all other cases

## Overall Verdict: INCONCLUSIVE

Mixed verdicts. ADX gates degrade Calmar. EMA200 neutral. Keep defaults OFF pending longer test.
