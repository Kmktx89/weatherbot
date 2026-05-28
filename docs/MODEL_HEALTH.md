# Model Health

_Generated 2026-05-28T03:14:06+00:00 · window 14d · auto-regenerated daily; do not hand-edit (see MODEL_CHANGES.md for the change journal)._

## Flags
- **ALERT** `dispersion_k_KXHIGHLAX` = 0.47 (k in [0.8, 1.25]; n=51) — interior-bucket dispersion (1.0 = calibrated)
- **ALERT** `dispersion_k_KXHIGHPHIL` = 0.57 (k in [0.8, 1.25]; n=51) — interior-bucket dispersion (1.0 = calibrated)
- **WATCH** `bias_drift_KXHIGHNY` = 0.54 (|Δ| <= 0.5°F; n=60) — deployed -0.44 fresh +0.10

## Opportunities
- none

## All readings

| metric | value | status | n | threshold | note |
|---|---|---|---|---|---|
| calibration_yes | 0.0 | OK | 56 | |gap| <= 5pp | pred 44.6% realized 44.6% |
| calibration_no_net_haircut | 7.7 | INSUFFICIENT_DATA | 16 | |residual after haircut| <= 5pp | NO known structural offset; pred 87.4% realized 68.8% net of 0.11 haircut |
| bias_drift_KXHIGHNY | 0.54 | WATCH | 60 | |Δ| <= 0.5°F | deployed -0.44 fresh +0.10 |
| bias_drift_KXHIGHCHI | 0.34 | OK | 60 | |Δ| <= 0.5°F | deployed -1.04 fresh -0.70 |
| bias_drift_KXHIGHMIA | -0.16 | OK | 59 | |Δ| <= 0.5°F | deployed -1.52 fresh -1.68 |
| bias_drift_KXHIGHLAX | 0.09 | OK | 59 | |Δ| <= 0.5°F | deployed -0.55 fresh -0.46 |
| bias_drift_KXHIGHDEN | 0.02 | OK | 59 | |Δ| <= 0.5°F | deployed -0.81 fresh -0.79 |
| bias_drift_KXHIGHAUS | -0.03 | OK | 60 | |Δ| <= 0.5°F | deployed -1.81 fresh -1.84 |
| bias_drift_KXHIGHPHIL | -0.34 | OK | 59 | |Δ| <= 0.5°F | deployed -1.22 fresh -1.56 |
| dispersion_k_KXHIGHNY | 0.99 | OK | 41 | k in [0.8, 1.25] | interior-bucket dispersion (1.0 = calibrated) |
| dispersion_k_KXHIGHCHI | 0.82 | OK | 51 | k in [0.8, 1.25] | interior-bucket dispersion (1.0 = calibrated) |
| dispersion_k_KXHIGHMIA | 0.91 | OK | 53 | k in [0.8, 1.25] | interior-bucket dispersion (1.0 = calibrated) |
| dispersion_k_KXHIGHLAX | 0.47 | ALERT | 51 | k in [0.8, 1.25] | interior-bucket dispersion (1.0 = calibrated) |
| dispersion_k_KXHIGHDEN | 0.91 | OK | 47 | k in [0.8, 1.25] | interior-bucket dispersion (1.0 = calibrated) |
| dispersion_k_KXHIGHAUS | 0.82 | OK | 47 | k in [0.8, 1.25] | interior-bucket dispersion (1.0 = calibrated) |
| dispersion_k_KXHIGHPHIL | 0.57 | ALERT | 51 | k in [0.8, 1.25] | interior-bucket dispersion (1.0 = calibrated) |
