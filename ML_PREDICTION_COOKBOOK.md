# ML prediction cookbook — small-N / rare-event problems

Portable checklist for building a predictive model when the outcome of interest
is rare (few positive events) relative to the number of rows and/or candidate
features. Copy this file into a new project's `CLAUDE.md` (or append it as a
section) when starting a similar analysis. Grounded in the `water_cut_predictor.py`
rewrite in abandoned-well-analysis, where the outcome is "well exits within 12
months" and positives are a small fraction of well-months.

## 0. Gate — decide if this is even modelable before touching a model

- Compute **events per variable (EPV)**: `EPV = n_positive_events / n_candidate_predictors`.
  Rule of thumb (Peduzzi et al. 1996): want EPV ≥ 10–20 per predictor in the
  model being fit. Below that, coefficient/estimate instability dominates and
  no amount of resampling or model sophistication fixes it — the fix is fewer
  predictors, a coarser question, more data, or an honest "can't reliably
  predict this" conclusion.
- Print N and event count next to every result, always. A metric without its
  denominator is not reportable.
- Build a trivial baseline (base rate, or the single strongest univariate
  predictor) before anything else. Every subsequent model must beat this
  **out-of-sample**, not in-sample.

## 1. Evaluation protocol — decide this before picking any model

- **Never evaluate on training data.** Fit and score are always on disjoint rows.
- **Group your splits if rows aren't independent.** Panel/longitudinal data
  (e.g. multiple well-months per well) must be split by entity
  (`GroupKFold` / `StratifiedGroupKFold` on well ID), not by row — otherwise the
  same well leaks across train/test and performance is inflated.
- **Repeat the outer split** (e.g. 5-fold × 3–5 repeats with different shuffles)
  to get a distribution of held-out scores, not a single lucky/unlucky number.
  With very small event counts, consider leave-one-group-out instead of k-fold.
- **Nest anything data-driven inside the outer loop**: hyperparameter tuning,
  feature scaling, class-imbalance resampling (e.g. negative subsampling for a
  rare positive class). Fit all of it only on the training fold; apply to the
  test fold, never fit on it. Skipping this ("fit once, then cross-validate" or
  "resample, then split") is the single most common source of optimistic
  results in small-N work.
- Class-imbalance handling (subsampling majority class, class weights) belongs
  **inside the training fold only**. The test fold keeps its natural class
  balance so the reported metric reflects reality.

## 2. Model ladder — pick 2–3 techniques on purpose, not a kitchen sink

Running every model family (linear + trees + boosting + SVM) and every
selection method (stepwise + subset + regularization) at once on a small,
rare-event dataset multiplies researcher-degrees-of-freedom — with few events,
"looks better" is often just noise. Instead:

- **Tier 1 — regularized linear/logistic regression.** Ridge or elastic-net,
  with the penalty strength chosen by nested CV. This replaces manual
  stepwise/best-subset selection — regularization *is* the feature selection,
  and unlike stepwise it doesn't produce unstable, overconfident coefficients.
  This is the explainable, always-report-this-one baseline.
- **Tier 2 — one flexible comparator.** Pick a *single* more flexible model
  family (gradient boosting is usually the better default over SVM for tabular
  data with rare events and a handful of features: native handling of
  nonlinearity/interactions, no kernel/scaling fragility, easy feature
  importances). Use small-N-appropriate hyperparameters (shallow trees, strong
  shrinkage, min-leaf-size floor) rather than a wide search — with only a few
  features there's little to tune, and a wide hyperparameter search is itself
  another multiple-comparisons risk.
- Do not add a third or fourth family unless Tier 2 already shows a real,
  stable edge over Tier 1. More models without evidence that they're needed is
  the kitchen-sink mistake, just spread across techniques instead of features.

## 3. Comparing models fairly

- Every model in the ladder is scored on **the same outer folds**, so
  differences reflect the model, not the split.
- Report both a **ranking metric** (AUC) and a **calibration metric**
  (Brier score / log-loss) — AUC can look fine while the actual probabilities
  are unusable, which matters a lot if downstream decisions (e.g. dollar
  thresholds) use the predicted probability itself, not just a rank.
- Compare **per-fold, paired** (Tier 2 score − Tier 1 score on the same fold),
  not just mean vs. mean. With small N, look at the win-rate across folds, not
  only the average — a mean edge driven by one fold is not a stable edge.
- AIC/BIC are only valid for comparing nested models within one GLM family fit
  without any data-driven search. They are not valid for comparing across
  model families (logistic vs. boosting) or after any feature/hyperparameter
  selection — use the nested-CV metric for that instead.

## 4. Escalation rule

Only keep/report the more flexible model if it beats Tier 1 by a margin that's
**consistent across folds** (higher mean *and* a paired win-rate meaningfully
above 50%). Otherwise report Tier 1 as the answer and say explicitly that the
more flexible model didn't earn its extra complexity — that's a legitimate,
useful conclusion, not a failure to find something better.

## 5. Reporting

- State N, event count, and EPV next to every metric.
- Report mean ± std (or the fold-level distribution) across folds/repeats, not
  a single point estimate — with small N the spread *is* part of the finding.
- For any final "deployed" model used to generate plots/thresholds, refit on
  all data using the CV-selected hyperparameters — but its reported
  performance number always comes from the held-out CV loop above, never from
  scoring this final refit on the same data it was trained on.

## 6. Judgment calls that are genuinely art-and-science

These don't have a single correct answer — flag them for a human decision
rather than picking silently:

- **Fold count / repeat count** given the actual event count (fewer events →
  fewer folds or leave-one-group-out, more repeats to compensate for split
  noise).
- **AUC vs. Brier/log-loss vs. adjusted-R²** as the headline number — depends
  on whether the end use is *ranking* (which wells are riskiest) or
  *calibration* (a probability feeds directly into a dollar threshold).
  Adjusted R² only applies to plain OLS with a fixed feature set — not once
  you're regularizing or comparing across model types.
- **Whether Tier 2's edge is "real"** — the win-rate/margin threshold that
  counts as "consistent enough to escalate" is a judgment call, not a fixed
  number; state the threshold you used and why.
