# σ=1.0 live validation — first cut

- **Date:** 2026-05-26
- **Branch:** `model-lab`
- **Method:** new `lab live-calibration` over `live_picks_log.jsonl` (hourly snapshot of the deployed LIVE_TODAY formula: NWS overlay + METAR today_max + σ=1.0), joined to settled outcomes. Window: last 7 days (2026-05-20 → 26).
- **Sample:** n=49 settled events (42 from the log's own `settled_bucket`, 7 via Kalshi fallback). Headline is the per-event prediction row nearest T-24h (mean actual lead 22.7h).

> **Sample-size caveat — read first.** n=49 events, 6 days, summer. The aggregate gaps below are directional. Band-level cells (n=6–18) sit inside their own ±15–35pp CIs. The decision-grade cut is the full 14-day window (~2026-06-03), run with the identical command. Nothing here is a verdict; the NO finding is alarming enough to act on now anyway.

---

## Headline: stand down on NO bets off the current dashboard

**You bet real money from these picks. The NO side is not safe to size off the dashboard or the current MODEL_NOTES guide.**

| Side | n | Model says (pred) | Reality (realized) | Gap | Backtest promised |
|---|---|---|---|---|---|
| YES | 49 | 44.3% | 44.9% | **+0.6pp** | +8.8pp |
| NO  | 43 | 76.3% | 46.5% | **−29.8pp** | −3.5pp |

- **YES is validated.** Live YES is essentially perfectly calibrated (+0.6pp) — even better than the backtest's promised +8.8pp underconfidence. Keep σ=1.0; trust the YES (highest-probability) picks at face value.
- **NO is ~8× worse than the backtest claimed.** The model says its NO picks win 76% of the time; they win 47%. At the leads you actually trade, the dashboard's "Best EV NO" is roughly a coin flip priced as a 3-to-1 favorite.

### NO calibration by printed-prob band (the bands MODEL_NOTES keys off)

| Printed NO prob | n | Pred | Realized | Gap | MODEL_NOTES currently says |
|---|---|---|---|---|---|
| ≥ 0.90 | 7 | 95% | 86% | **−9.4pp** | "take, subtract 3pp" |
| 0.75–0.90 | 18 | 83% | 67% | **−16.0pp** | "take, subtract 3-5pp" |
| 0.60–0.75 | 12 | 69% | 17% | **−52.4pp** | "SKIP — danger zone" |
| < 0.60 | 6 | 50% | 0% | **−50.0pp** | "SKIP" |

Two things at once:
1. **The skip rules are confirmed and if anything understated.** The 0.60–0.75 "danger zone" realized 17% (the guide warned ~30pp overconfident; live is −52pp). The <0.60 band went 0-for-6. Keep skipping these.
2. **The "take" bands are also overconfident** — 2–4× more than the guide's "subtract 3–5pp." Even ≥0.90 NO bets are ~−9pp. The guide's adjustment table is too optimistic for live.

### Lead degradation (all pred rows binned, directional)

| Lead | YES gap | NO gap |
|---|---|---|
| [0–12)h | +6.7pp | −34.4pp |
| [12–24)h | −2.9pp | −27.1pp |
| [24–36)h | −2.2pp | −28.1pp |
| [36+)h | −16.6pp | −16.9pp |

NO is least-bad far from close and degrades toward settlement (the replay leadtime report predicted this qualitatively; live confirms it, with the whole curve shifted ~25pp worse). today_max contamination near close (NO −34pp at [0–12)h) is the worst regime — but note NO is already −27 to −28pp at T-24h, *before* today_max fires on the resolution day, so today_max is an aggravator, not the root cause.

---

## Root cause: the NO strategy adverse-selects against the market

The selection rule is identical in backtest and live (`argmax ev_no` after the sanity cap + `MIN_BEST_EV`; verified `lab/replay.py:_eval_no` vs `kalshi_temp.py:predict_event`). So the gap is *not* a methodology artifact — it is the unreplayable NWS + today_max formula meeting a structural flaw the backtest under-detected:

- `ev_no = (1 − p_yes) − no_ask` is **maximized exactly when `no_ask` is cheapest** — i.e. when the *market* is most confident the bucket is YES. The NO strategy therefore systematically bets against the market's strongest convictions.
- The sanity cap (`yes_ask ≥ 0.85 AND p_yes ≤ 0.40`) only catches this when the model *also* rates the bucket unlikely. It misses the dangerous middle: hand-checked losers bet NO on buckets the model itself rated **p_yes = 0.55–0.61** (its own favorites) purely because `no_ask` was 0.06–0.12. Examples: NY-26MAY21 (NO on "67° or below", p_yes 0.58, lost), LAX-26MAY20 (NO on "77° or above", p_yes 0.61, lost). The market had intraday information the static forecast lacked, and was right.
- The backtest under-detected this because its sample, frozen forecasts, and historical candle prices produced less extreme `no_ask` adverse selection. Live exposes it.

This is the "unreplayable gap" the divergence-attribution report (2026-05-19) predicted: the live-vs-backtest divergence lives entirely in NWS + today_max, and it lands on the NO side.

Supporting (low weight): shadow-summary of the three minus-variants (YES-only, n≈42 each, CI-overlapping) ranks `live-minus-push` highest (WR 52.4% / +$2.87 vs full-formula proxies ~43%). Directionally consistent with the today_max **push** being a contributor; not strong enough to act on alone.

---

## Three separate decisions (do not conflate)

1. **σ=1.0 cutover → VALIDATED.** Keep it. YES is well-calibrated; σ neither causes nor fixes the NO problem.
2. **Merge `model-lab` → `main` → not blocked by this finding.** The lab tooling worked, σ=1.0 is good, the code is sound. Condition: the MODEL_NOTES correction below ships *with* the merge, not after, so the money-facing guide is never live-but-wrong.
3. **NO betting off the current dashboard → STAND DOWN.** Until the NO selection is fixed and re-validated on the 06-03 cut. Interim rule if you must: take NO **only** at printed prob ≥ 0.90, and even then size as if the true edge is ~10pp thinner than printed; skip everything < 0.90.

---

## What this changes now

- **MODEL_NOTES updated** (this commit): NO section gets a live-validation warning at the top; the "take" band adjustment numbers are *pulled* (not rewritten — n too small to assert new numbers); skip rules kept and flagged as confirmed.
- **Follow-up spec (named, not done): fix the NO adverse-selection.** Options to test: (a) widen `_sanity_keep_no` to skip when `no_ask < ~0.20` regardless of p_yes, or require `p_yes ≤ 0.40` to ever surface a NO; (b) floor the surfaced NO on printed prob ≥ 0.80; (c) test `today_max_mode="truncate"` (drop the +0.3 push). Validate each against the live log, not the archive.

## What to re-run at the 06-03 decision cut (identical commands)

```
python -m lab.cli live-calibration --days 14
python -m lab.cli live-calibration --days 14 --by-lead
```

Expect tighter CIs on the band table. If the NO "take" bands stay overconfident at n≈100, write the corrected adjustment numbers into MODEL_NOTES then — not before.

## Limitations

- n=49 events / 6 days / summer regime. Band cells n=6–18.
- Per-event headline uses the row nearest T-24h; mean actual lead 22.7h (range 5.9–31.9h), so a few events are scored closer to close than T-24.
- Lead-binned cuts pool multiple rows per event (not independent) — directional only.
- shadow-summary variants are YES-only and CI-overlapping.
