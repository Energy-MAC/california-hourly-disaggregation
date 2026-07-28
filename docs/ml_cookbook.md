# Machine-learning prediction cookbook

Full detail. The README carries a one-paragraph summary; this document holds the
cookbook design, the two configurations, the key results, and the cold-start
imputation. This is **not** a disaggregation "Approach" — it is a reusable, shared
method (`src/ml/`) that other predictive targets reuse unchanged.

A reusable, target-agnostic ML methodology so every predictive model in the project
is built and evaluated the same way: leakage-safe splitting, declarative feature
specs, a model registry with tuning spaces, a fixed metric suite, and standard
diagnostics — orchestrated by `run_cookbook()`. Other targets (e.g. future EIA-930
forecasting, via the `TimeSeriesSplit` re-export in `src/ml/splits.py`) reuse it
unchanged.

## First application — cross-sectional substation load

`scripts/load_projection/ml/predict_substation_load.py`. The substation profiles are
month×hour percentile envelopes, **not time series**, so this is cross-sectional
regression: predict a substation's per-cell `max_load`/`min_load` from structural +
calendar features. **Validation holds out whole substations, never random cells** —
the cross-sectional analogue of a time-based split. Random k-fold would let a model
memorize each substation's level and massively overstate accuracy.

Two configurations of the same cookbook:

- **explanatory** — all features incl. SCE-only attributes (customer mix, DER,
  projected load) + diurnal-neighbour lags; group hold-out by substation. Answers
  *"given part of a substation, predict the rest."*
- **imputable** — only features that also exist for substations with **no** profile
  (location, `highside_kv` + CATS voltage class, county population / `ca_load_fraction`
  / BTM-PV, calendar; **no lags**); spatial-block hold-out. The configuration
  applicable to the ~1,000+ load-eligible unscraped CEC substations.

Models: baselines (`global_mean`, `cell_mean`) + `linear`/`ridge`/`lasso`/`elasticnet`
+ `arx_ols` + `svr` (RBF, ≤15k rows) + `hist_gbm`/`xgboost`/`lightgbm`. Metrics: RMSE,
MAE, R², **WAPE** (MAPE excluded — loads go negative/near-zero from BTM reverse flow),
peak-cell MAE, and **skill vs the `cell_mean` baseline**; segmented by
hour/month/utility/voltage class.

**Key result — the whole methodological point.** On a representative run (~520
substations; full fleet via the same command without `--max-rows`), the explanatory
config reaches **R² ≈ 0.61 / skill ≈ 0.37** vs cell-mean — a cell's neighbouring-hour
load, the strongest single feature, correlates only ≈0.82 with it, so lags help a lot
but aren't decisive (linear/ARX slightly edge the boosters). The imputable config drops
to **R² ≈ 0.12 / skill ≈ 0.06** (boosting best) — cold-start prediction of a
substation's absolute *magnitude* from location/county/voltage is weak. That drop (0.37
→ 0.06) is the honest, important point: reporting the explanatory number as the
imputation number would overstate what the model can do for a genuinely unseen
substation. Structural features can place a substation in space and shape but not
*size* it.

Outputs: `data/checks/ml/substation_load/comparison_{config}.csv` (+ `segmented_errors_*`,
`tuned_params_*`); figures in `data/figures/ml/substation_load/`. Leakage +
imputable-feasibility guards: `test_leakage_guards.py`.

```bash
python scripts/load_projection/ml/predict_substation_load.py           # both configs, all models
python scripts/load_projection/ml/predict_substation_load.py --config imputable --target min_load
python scripts/load_projection/ml/test_leakage_guards.py
```

## Cold-start imputation onto unscraped substations (SCE first)

`impute_unscraped_load.py` (+ `src/ml/imputation.py`) produces a 288-cell profile for
each of the **688** sub-500 kV unscraped SCE substations via a **magnitude × shape
decomposition** with empirical anchoring — because the direct per-cell model is weak
(imputable skill ≈0.06):

- **Magnitude** `M = mean_c(max_load)` — from a cookbook regressor **or** k-NN median
  (chosen per held-out validation).
- **Shape** — normalized 288-cell templates from k-NN donors or a voltage-class group
  average (chosen by validation). `profile = M̂ · shape`.

Validation holds out whole **spatial blocks** of scraped SCE substations and reports
the three facets **separately**:

| Facet | Result (held-out scraped SCE) |
|-------|-------------------------------|
| **Magnitude** | k-NN median MAE ≈13.6 MW, R²≈−0.04 (beats the tree, which overfits geography). Rich SCE features roughly halve MAE (16.4→9.3) but R² stays ≈0, and `projected_load` adds almost nothing (not a circular oracle). |
| **Shape** | voltage-class group template median correlation ≈**0.55** (max) / 0.53 (min), beating k-NN donor (0.46/0.49). |
| **Combined per-cell** | imputed MAE ≈13.8 vs naive-baseline 20.5, but RMSE barely moves (38.0 vs 38.2) → **skill ≈ 0**. |

**Honest conclusion:** imputation recovers the *shape* well but the *magnitude* poorly
— held-out rural substations that actually carry ≈0 load still get sized at 10–20 MW,
because location/voltage cannot reveal they are tiny. The deliverable is
**right-shape, uncertain-magnitude** profiles with a donor-spread band, not accurate
reconstructions. A usable magnitude needs a direct size proxy or conservation to county
totals (deferred). The imputed profiles are a **standalone artifact** — not wired into
the nodal mapping / projection pipeline.

Outputs: `data/processed/ml/imputed_substation_profiles_sce.csv` (688 subs × 288 cells),
validation tables in `data/checks/ml/imputation/`, figures in
`data/figures/ml/imputation/`.

```bash
python scripts/load_projection/ml/impute_unscraped_load.py                  # SCE, validate + deploy
python scripts/load_projection/ml/impute_unscraped_load.py --no-deploy      # validation/ceiling only
```
