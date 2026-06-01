# Model Health

_Generated 2026-06-01T04:05:02+00:00 · window 14d · auto-regenerated daily; do not hand-edit (see MODEL_CHANGES.md for the change journal)._

> ⚠️ **Deployed model changed since last run** — add a `docs/MODEL_CHANGES.md` entry (what/why/validation/deployed-or-held).

## Flags
- **ALERT** `dispersion_k_KXHIGHLAX` = 0.4 (k in [0.8, 1.25]; n=51) — interior-bucket dispersion, archive/backtest formula (1.0 = calibrated)
- **ALERT** `dispersion_k_KXHIGHPHIL` = 0.61 (k in [0.8, 1.25]; n=51) — interior-bucket dispersion, archive/backtest formula (1.0 = calibrated)
- **WATCH** `bias_drift_KXHIGHNY` = 0.63 (|Δ| <= 0.5°F; n=59) — deployed -0.44 fresh +0.19
- **WATCH** `dispersion_k_KXHIGHAUS` = 0.8 (k in [0.8, 1.25]; n=46) — interior-bucket dispersion, archive/backtest formula (1.0 = calibrated)

## Opportunities
- none

## All readings

| metric | value | status | n | threshold | note |
|---|---|---|---|---|---|
| calibration_yes | -3.6 | OK | 75 | |gap| <= 5pp | pred 45.0% realized 41.3% |
| calibration_no_net_haircut | 15.5 | INSUFFICIENT_DATA | 21 | |residual after haircut| <= 5pp | NO known structural offset; pred 88.4% realized 61.9% net of 0.11 haircut |
| bias_drift_KXHIGHNY | 0.63 | WATCH | 59 | |Δ| <= 0.5°F | deployed -0.44 fresh +0.19 |
| bias_drift_KXHIGHCHI | 0.49 | OK | 59 | |Δ| <= 0.5°F | deployed -1.04 fresh -0.55 |
| bias_drift_KXHIGHMIA | -0.29 | OK | 58 | |Δ| <= 0.5°F | deployed -1.52 fresh -1.81 |
| bias_drift_KXHIGHLAX | 0.02 | OK | 59 | |Δ| <= 0.5°F | deployed -0.55 fresh -0.53 |
| bias_drift_KXHIGHDEN | -0.12 | OK | 59 | |Δ| <= 0.5°F | deployed -0.81 fresh -0.93 |
| bias_drift_KXHIGHAUS | 0.06 | OK | 60 | |Δ| <= 0.5°F | deployed -1.81 fresh -1.75 |
| bias_drift_KXHIGHPHIL | -0.44 | OK | 59 | |Δ| <= 0.5°F | deployed -1.22 fresh -1.66 |
| dispersion_k_KXHIGHNY | 0.98 | OK | 41 | k in [0.8, 1.25] | interior-bucket dispersion, archive/backtest formula (1.0 = calibrated) |
| dispersion_k_KXHIGHCHI | 0.87 | OK | 51 | k in [0.8, 1.25] | interior-bucket dispersion, archive/backtest formula (1.0 = calibrated) |
| dispersion_k_KXHIGHMIA | 0.82 | OK | 51 | k in [0.8, 1.25] | interior-bucket dispersion, archive/backtest formula (1.0 = calibrated) |
| dispersion_k_KXHIGHLAX | 0.4 | ALERT | 51 | k in [0.8, 1.25] | interior-bucket dispersion, archive/backtest formula (1.0 = calibrated) |
| dispersion_k_KXHIGHDEN | 0.92 | OK | 47 | k in [0.8, 1.25] | interior-bucket dispersion, archive/backtest formula (1.0 = calibrated) |
| dispersion_k_KXHIGHAUS | 0.8 | WATCH | 46 | k in [0.8, 1.25] | interior-bucket dispersion, archive/backtest formula (1.0 = calibrated) |
| dispersion_k_KXHIGHPHIL | 0.61 | ALERT | 51 | k in [0.8, 1.25] | interior-bucket dispersion, archive/backtest formula (1.0 = calibrated) |
