"""Single source of truth for weatherbot selection / calibration / health
thresholds.

Pure leaf module: it imports NOTHING (kalshi_temp and the lab/* modules import
each other, so a constants module that pulled either in would create a cycle).
Everything else imports FROM here and never redefines these literals — so the
dashboard backtest model, the live pick path, and the daily health scan share
one definition. See the weatherbot-backtest-harness skill for the contract.
"""

# ---- selection / EV gating (live + backtest) ----
MIN_BEST_EV = 0.05            # calibrated-EV floor (>=5c) to "take" a pick

# ---- NO-side sanity rules (live dashboard + backtest) ----
SANITY_MARKET_CONFIDENT_YES = 0.85   # suppress NO when yes_ask >= 85c
SANITY_MODEL_LOW_PROB = 0.40         # ...and model prob <= 40c (don't fight the book)
MIN_PRINTED_NO = 0.80                # printed-NO (1 - p_yes) floor (2026-05-26 fix)

# ---- forecast uncertainty ----
BASE_SIGMA = 1.0              # sigma floor (degF); graduated 2026-05-19 from 2.0

# ---- NO calibration haircut (band [MIN_PRINTED_NO, 1.01] -> subtract NO_HAIRCUT) ----
NO_HAIRCUT = 0.11            # 2026-05-26 live calibration cut for printed-NO >= 0.80

# ---- health-scan classification thresholds (lab/health) ----
MIN_N = 30                   # below this a metric reports INSUFFICIENT_DATA
CAL_WATCH_PP = 5.0           # calibration gap: |gap| <= 5pp = OK
CAL_ALERT_PP = 10.0          # |gap| > 10pp = ALERT (between = WATCH)
K_OK = (0.8, 1.25)           # dispersion k OK band (1.0 = calibrated)
K_WATCH = (0.65, 1.4)        # dispersion k WATCH band (outside = ALERT)
BIAS_WATCH = 0.5             # bias drift |delta| <= 0.5degF = OK
BIAS_ALERT = 1.0             # |delta| > 1.0degF = ALERT (between = WATCH)
OPP_EDGE_MIN = 0.05          # unexploited-edge opportunity floor (+5pp)
BIAS_RESID_WATCH = 0.5       # live-path bias residual |mean| <= 0.5degF = OK
BIAS_RESID_ALERT = 1.5       # |mean| > 1.5degF = ALERT (between = WATCH; looser than replay drift — live path is noisier)
BIAS_RESID_POOLED_MIN_N = 30 # pooled live-residual sufficiency gate
