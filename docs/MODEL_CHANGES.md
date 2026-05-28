# Model change journal

Append-only. One entry per deployed/held model change. Newest at the bottom.
The daily health scan appends a stub here when it detects the deployed model
changed; fill the stub in. Template:

## YYYY-MM-DD — <short title>
- change: <what changed>
- why: <motivation / the finding it addresses>
- validation: <gates run + results>
- deployed-or-held: <deployed (commit) | held (reason)>
- commit: <sha>

---

## 2026-05-27 — weighted-source sigma
- change: `_sigma_from` now weights the source spread by mu's effective weights instead of an unweighted pstdev.
- why: sigma over-weighted sources mu suppresses (LAX ECMWF +6F at weight 0.10) -> LAX ~4x over-dispersed.
- validation: dispersion re-run (LAX interior k 0.36->0.46, sigma 3.83->2.43; DEN control exact; pooled 0.83->0.85); mu unchanged (baselines regenerated); full suite green; opus final review.
- deployed-or-held: deployed; live LAX sigma=1.236 confirmed = weighted formula.
- commit: c60d193 (merged to model-lab a06d3d0)
