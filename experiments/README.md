# Experiments — pre-registration required

Every champion/challenger experiment gets a file here **committed BEFORE the
challenger is enabled**. This is the guard against the overfitting half of the
evaluation dilemma: deciding what "better" means *after* seeing the results is
how a lucky run becomes a "confirmed improvement". The file is cheap; the
discipline is the point.

## How to run an experiment

1. Copy `TEMPLATE.md` to `exp-YYYY-MM-<slug>.md` and fill in every field.
2. Commit it.
3. Set the challenger on the trading-service container:
   `CHALLENGER_SPEC='{"label": "exp-YYYY-MM-<slug>", "custom_instructions": "..."}'`
   (the label MUST match the filename).
4. Let cycles run. Pairs accrue in `challenger_decisions`; disagreements
   resolve on the same 7-day ±1% contract as champion decisions.
5. Watch `GET /api/v1/challenger/stats?label=...`. The e-value is
   **anytime-valid**: peek after every resolved pair if you like, stop the
   moment it crosses the threshold you pre-registered. That is legal here, by
   construction — it is NOT legal with a classical p-value.
6. When the stop rule fires (either direction), record the result in the
   experiment file, disable the challenger, and only then decide promotion.

## Interpretation contract

- `e_value >= 20` → challenger better at α=0.05 (>= 100 → α=0.01).
- `e_value < 1` with ≥5 informative pairs → evidence favours no-difference.
- `regressing_sectors` non-empty → do NOT promote on the aggregate alone,
  investigate the slice first ("better on average, broken somewhere").
- Agreement rate is a sanity check, not a verdict: ~100% agreement means the
  change does nothing observable; very low agreement means it changes far more
  than intended.

## The rules that make results meaningful

- One active experiment at a time (one CHALLENGER_SPEC).
- Never edit the hypothesis/threshold fields after data starts arriving; if
  the design was wrong, close the experiment and register a new one.
- The decision-variance noise floor (scripts/decision_variance.py) bounds what
  is detectable: if the same desk flips actions 20% of the time, agreement
  below 80% is expected from noise alone.
- The grounding-judge metrics are the Goodhart guard: they are never a target,
  only a tripwire. If challenger scores climb while grounding falls, the
  challenger is gaming the scorer, not improving.
