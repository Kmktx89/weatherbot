# Model Health

_Generated 2026-06-01T18:04:10+00:00 · window 14d · auto-regenerated daily; do not hand-edit (see MODEL_CHANGES.md for the change journal)._

> ⚠️ **Deployed model changed since last run** — add a `docs/MODEL_CHANGES.md` entry (what/why/validation/deployed-or-held).

## Flags
- **ALERT** `dispersion_k_KXHIGHLAX` = 0.4 (k in [0.8, 1.25]; n=52) — interior-bucket dispersion, archive/backtest formula (1.0 = calibrated)
- **ALERT** `dispersion_k_KXHIGHPHIL` = 0.6 (k in [0.8, 1.25]; n=52) — interior-bucket dispersion, archive/backtest formula (1.0 = calibrated)
- **WATCH** `bias_drift_KXHIGHNY` = 0.67 (|Δ| <= 0.5°F; n=60) — deployed -0.44 fresh +0.23

## Opportunities
- none

## All readings

| metric | value | status | n | threshold | note |
|---|---|---|---|---|---|
| calibration_yes | -3.6 | OK | 75 | |gap| <= 5pp | pred 45.0% realized 41.3% |
| calibration_no_net_haircut | 15.5 | INSUFFICIENT_DATA | 21 | |residual after haircut| <= 5pp | NO known structural offset; pred 88.4% realized 61.9% net of 0.11 haircut |
| bias_drift_KXHIGHNY | 0.67 | WATCH | 60 | |Δ| <= 0.5°F | deployed -0.44 fresh +0.23 |
| bias_drift_KXHIGHCHI | 0.5 | OK | 60 | |Δ| <= 0.5°F | deployed -1.04 fresh -0.54 |
| bias_drift_KXHIGHMIA | -0.29 | OK | 59 | |Δ| <= 0.5°F | deployed -1.52 fresh -1.81 |
| bias_drift_KXHIGHLAX | 0.02 | OK | 60 | |Δ| <= 0.5°F | deployed -0.55 fresh -0.53 |
| bias_drift_KXHIGHDEN | -0.14 | OK | 60 | |Δ| <= 0.5°F | deployed -0.81 fresh -0.95 |
| bias_drift_KXHIGHAUS | 0.11 | OK | 60 | |Δ| <= 0.5°F | deployed -1.81 fresh -1.70 |
| bias_drift_KXHIGHPHIL | -0.45 | OK | 60 | |Δ| <= 0.5°F | deployed -1.22 fresh -1.67 |
| dispersion_k_KXHIGHNY | 1.04 | OK | 42 | k in [0.8, 1.25] | interior-bucket dispersion, archive/backtest formula (1.0 = calibrated) |
| dispersion_k_KXHIGHCHI | 0.86 | OK | 52 | k in [0.8, 1.25] | interior-bucket dispersion, archive/backtest formula (1.0 = calibrated) |
| dispersion_k_KXHIGHMIA | 0.82 | OK | 51 | k in [0.8, 1.25] | interior-bucket dispersion, archive/backtest formula (1.0 = calibrated) |
| dispersion_k_KXHIGHLAX | 0.4 | ALERT | 52 | k in [0.8, 1.25] | interior-bucket dispersion, archive/backtest formula (1.0 = calibrated) |
| dispersion_k_KXHIGHDEN | 0.92 | OK | 48 | k in [0.8, 1.25] | interior-bucket dispersion, archive/backtest formula (1.0 = calibrated) |
| dispersion_k_KXHIGHAUS | 0.8 | OK | 46 | k in [0.8, 1.25] | interior-bucket dispersion, archive/backtest formula (1.0 = calibrated) |
| dispersion_k_KXHIGHPHIL | 0.6 | ALERT | 52 | k in [0.8, 1.25] | interior-bucket dispersion, archive/backtest formula (1.0 = calibrated) |
