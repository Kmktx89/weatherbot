# weatherbot — repo session guidance

At the start of any weatherbot **model** session, before proposing model changes:
1. Read `docs/MODEL_HEALTH.md` (auto-regenerated daily by the WeatherbotHealth task) — current calibration / dispersion / bias-drift flags + unexploited-edge opportunities.
2. Skim `docs/MODEL_CHANGES.md` — the change journal (what's been tried, what helped, what was held).

The health loop is detection/reporting only; any change still goes through the gated pipeline (TDD -> spec-and-code review -> validation -> human-gated deploy). See `docs/superpowers/specs/2026-05-27-model-health-loop-design.md`.
