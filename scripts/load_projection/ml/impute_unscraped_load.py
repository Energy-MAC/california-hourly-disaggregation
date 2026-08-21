"""LEGACY (2026-08-14) -- not part of the project's method; see docs/ml_cookbook.md.

Imputing profiles for substations we have no data for is a PREDICTION problem;
the project's question is the impact of disaggregating a KNOWN load. This output
(data/processed/ml/imputed_substation_profiles_sce.csv) is a standalone artifact
and is deliberately NOT consumed by the nodal mapping or the GenX rescaling --
the validation below is exactly why: the method recovers profile shape but not
magnitude.

--------------------------------------------------------------------------

Cold-start load-profile imputation for unscraped CEC substations (SCE first).

Produces a 288-cell (month x hour) min_load/max_load profile for each
load-eligible unscraped SCE substation, via a MAGNITUDE x SHAPE decomposition
with empirical anchoring (see docs/plan). Because both magnitude and shape are
only weakly predictable from the features an unscraped substation has, the point
is honest, SEPARATED validation, plus a rich-feature CEILING study.

Runs, all on the same spatial hold-out of scraped SCE substations:
  1. Magnitude validation + ceiling -- predict per-substation size M from
     structural features across three tiers (imputable / rich_no_projected /
     rich), reusing the cookbook models. `projected_load` is a near-circular
     size proxy, hence the middle tier isolates genuine structural lift.
  2. Shape validation -- k-NN donor vs group-average normalized templates.
  3. Combined per-cell -- deployable pipeline (imputable magnitude x k-NN shape)
     vs a naive baseline (global mean shape x median magnitude).
Then deploys (imputable magnitude + k-NN shape from all scraped SCE) onto the
688 unscraped SCE targets.

CLI:
  --utility {sce,pge,sdge}  default sce
  --k INT                   donor neighbours for shape/knn-magnitude (default 15)
  --magnitude-model NAME    cookbook model for M (default hist_gbm)
  --n-iter INT              tuning iterations (default 15)
  --seed INT                (default 20260727)
  --no-deploy               validation only (skip writing imputed profiles)

Outputs:
  data/processed/ml/imputed_substation_profiles_{util}.csv
  data/checks/ml/imputation/{magnitude,shape,percell,ceiling}_validation.csv
  data/figures/ml/imputation/{ceiling_bar,shape_corr,example_profiles}.png

Usage:
  python scripts/load_projection/ml/impute_unscraped_load.py
  python scripts/load_projection/ml/impute_unscraped_load.py --k 20 --no-deploy
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/ml"))
from ml import evaluate as ev  # noqa: E402
from ml import imputation as im  # noqa: E402
from ml.models import build_registry  # noqa: E402
from ml.splits import spatial_blocks  # noqa: E402
from ml.tuning import tune_or_fit  # noqa: E402
from sklearn.impute import SimpleImputer  # noqa: E402
from sklearn.neighbors import NearestNeighbors  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402
from substation_features import (IMPUTABLE_STRUCT, feature_tiers,  # noqa: E402
                                 scraped_structural, unscraped_structural)

PROF_FILE = ROOT / "data/processed/substations/substation_load_profiles_clean.csv"
OUT_PROC = ROOT / "data/processed/ml"
OUT_CHECKS = ROOT / "data/checks/ml/imputation"
OUT_FIG = ROOT / "data/figures/ml/imputation"
CELL = ["month", "hour"]


def load_scraped(utility: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(long profiles with substation_id, per-substation structural features)."""
    prof = pd.read_csv(PROF_FILE, usecols=["utility", "substation_name", "month",
                                           "hour_pst", "min_load", "max_load"]).rename(
        columns={"hour_pst": "hour"})
    prof = prof[prof.utility == utility].dropna(subset=["max_load"]).copy()
    prof["substation_id"] = prof.utility + "|" + prof.substation_name
    struct = scraped_structural(utility)
    struct["substation_id"] = struct.utility + "|" + struct.substation_name
    # keep only substations present in BOTH (a profile -> magnitude, and features)
    common = set(prof.substation_id) & set(struct.substation_id)
    prof = prof[prof.substation_id.isin(common)].copy()
    struct = struct[struct.substation_id.isin(common)].drop_duplicates("substation_id").reset_index(drop=True)
    return prof, struct


def spatial_test_ids(struct: pd.DataFrame, seed: int, n_blocks: int = 10,
                     test_frac: float = 0.25) -> set[str]:
    """Whole-substation spatial hold-out (blocks of substations held out together)."""
    blocks = spatial_blocks(struct[["lat", "lon"]], n_blocks, seed)
    valid = [b for b in blocks.unique() if b >= 0]
    rng = np.random.default_rng(seed)
    n_test = max(1, int(round(len(valid) * test_frac)))
    test_blocks = set(rng.choice(valid, size=n_test, replace=False).tolist())
    return set(struct.substation_id[blocks.isin(test_blocks)])


def fit_predict_magnitude(struct: pd.DataFrame, mag: pd.Series, feat: list[str],
                          train_ids, target_struct: pd.DataFrame, model: str,
                          n_iter: int, seed: int):
    """Fit the magnitude model on train substations, predict M for target_struct."""
    tr = struct[struct.substation_id.isin(train_ids)]
    y = mag.reindex(tr.substation_id).to_numpy()
    factory, space = build_registry()[model]
    groups = pd.Series(tr.substation_id.to_numpy())  # 1 row/substation
    est, _ = tune_or_fit(factory, space, tr[feat], y, groups, n_splits=5,
                         n_iter=n_iter, scoring="neg_root_mean_squared_error", seed=seed)
    return est.predict(target_struct[feat])


def knn_median_magnitude(struct, mag, train_ids, target_struct, feat, k):
    tr = struct[struct.substation_id.isin(train_ids)]
    imp, sc = SimpleImputer(strategy="median"), StandardScaler()
    Xtr = sc.fit_transform(imp.fit_transform(tr[feat]))
    Xt = sc.transform(imp.transform(target_struct[feat]))
    nn = NearestNeighbors(n_neighbors=min(k, len(tr))).fit(Xtr)
    _, idx = nn.kneighbors(Xt)
    mtr = mag.reindex(tr.substation_id).to_numpy()
    return np.nanmedian(mtr[idx], axis=1)


def predict_magnitude(kind, struct, mag, feat, train_ids, target_struct, args):
    """Dispatch the deployable magnitude estimator (imputable features)."""
    if kind == "knn":
        return knn_median_magnitude(struct, mag, train_ids, target_struct, feat, args.k)
    return fit_predict_magnitude(struct, mag, feat, train_ids, target_struct,
                                 args.magnitude_model, args.n_iter, args.seed)


def build_shape_template(kind, target_struct, donor_struct, donor_shape_long, k):
    """Return ({value -> target x 288 template}, donor_spread) for the chosen kind.
    Donor spread (k-NN based) is always computed for the uncertainty band."""
    knn_t, spread = im.knn_donor_templates(target_struct, donor_struct, donor_shape_long,
                                           IMPUTABLE_STRUCT, "substation_id", k)
    if kind == "knn":
        return knn_t, spread
    grp_map = donor_struct.set_index("substation_id").sub_kv_class.astype("string").fillna("na")
    tgt_grp = target_struct.set_index("substation_id").sub_kv_class.astype("string").fillna("na")
    return im.group_templates(donor_shape_long, grp_map, tgt_grp, "substation_id"), spread


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--utility", choices=["sce", "pge", "sdge"], default="sce")
    ap.add_argument("--k", type=int, default=15)
    ap.add_argument("--magnitude-model", default="hist_gbm",
                    help="cookbook regressor used for the 'regressor' magnitude estimator")
    ap.add_argument("--magnitude", choices=["regressor", "knn", "auto"], default="auto",
                    help="deployable magnitude estimator; auto picks the better on held-out")
    ap.add_argument("--shape", choices=["knn", "group", "auto"], default="auto",
                    help="shape template; auto picks the better on held-out")
    ap.add_argument("--n-iter", type=int, default=15)
    ap.add_argument("--seed", type=int, default=20260727)
    ap.add_argument("--no-deploy", action="store_true")
    args = ap.parse_args()
    for d in (OUT_PROC, OUT_CHECKS, OUT_FIG):
        d.mkdir(parents=True, exist_ok=True)

    prof, struct = load_scraped(args.utility)
    mag, shape_long = im.decompose(prof)
    tiers = feature_tiers()
    test_ids = spatial_test_ids(struct, args.seed)
    train_ids = set(struct.substation_id) - test_ids
    tr_struct = struct[struct.substation_id.isin(train_ids)]
    te_struct = struct[struct.substation_id.isin(test_ids)]
    print(f"{args.utility}: {len(struct)} scraped substations "
          f"({len(train_ids)} train / {len(test_ids)} spatial-holdout test)")

    # ── 1. magnitude validation + ceiling ───────────────────────────────────
    m_true = mag.reindex(te_struct.substation_id).to_numpy()
    mag_rows = []
    for tier, feat in tiers.items():
        m_hat = fit_predict_magnitude(struct, mag, feat, train_ids, te_struct,
                                      args.magnitude_model, args.n_iter, args.seed)
        mag_rows.append({"tier": tier, "estimator": args.magnitude_model,
                         "n_features": len(feat), **ev.core_metrics(m_true, m_hat)})
    # k-NN median (imputable only) + a global-mean baseline for skill
    m_knn = knn_median_magnitude(struct, mag, train_ids, te_struct, tiers["imputable"], args.k)
    mag_rows.append({"tier": "imputable", "estimator": "knn_median",
                     "n_features": len(tiers["imputable"]), **ev.core_metrics(m_true, m_knn)})
    m_base = np.full(len(m_true), mag.reindex(tr_struct.substation_id).median())
    mag_rows.append({"tier": "baseline", "estimator": "train_median",
                     "n_features": 0, **ev.core_metrics(m_true, m_base)})
    mag_val = pd.DataFrame(mag_rows)
    mag_val.to_csv(OUT_CHECKS / "magnitude_validation.csv", index=False)
    print("\n=== MAGNITUDE (held-out; ceiling across feature tiers) ===")
    print(mag_val.to_string(index=False))

    # ── 2. shape validation (k-NN donor vs group template) ───────────────────
    tr_shape = shape_long[shape_long.substation_id.isin(train_ids)]
    knn_tmpl, spread = im.knn_donor_templates(te_struct, tr_struct, tr_shape,
                                              IMPUTABLE_STRUCT, "substation_id", args.k)
    grp_map = tr_struct.set_index("substation_id").sub_kv_class.astype("string").fillna("na")
    tgt_grp = te_struct.set_index("substation_id").sub_kv_class.astype("string").fillna("na")
    grp_tmpl = im.group_templates(tr_shape, grp_map, tgt_grp, "substation_id")

    shape_rows = []
    te_shape = shape_long[shape_long.substation_id.isin(test_ids)]
    for value in ("max_shape", "min_shape"):
        true_w = im._shape_wide(te_shape, "substation_id", value)
        for name, tmpl in [("knn_donor", knn_tmpl), ("group", grp_tmpl)]:
            sc = im.shape_scores(tmpl[value], true_w)
            shape_rows.append({"envelope": value, "template": name, "n": len(sc),
                               "median_corr": sc.shape_corr.median(),
                               "median_nrmse": sc.shape_nrmse.median()})
    shape_val = pd.DataFrame(shape_rows)
    shape_val.to_csv(OUT_CHECKS / "shape_validation.csv", index=False)
    print("\n=== SHAPE (held-out; normalized-profile recovery) ===")
    print(shape_val.to_string(index=False))

    # ── auto-select deployable methods from the held-out validation ──────────
    if args.magnitude == "auto":
        imp_reg = mag_val[(mag_val.tier == "imputable") & (mag_val.estimator == args.magnitude_model)].rmse.iloc[0]
        imp_knn = mag_val[mag_val.estimator == "knn_median"].rmse.iloc[0]
        best_mag = "knn" if imp_knn <= imp_reg else "regressor"
    else:
        best_mag = args.magnitude
    if args.shape == "auto":
        mx = shape_val[shape_val.envelope == "max_shape"].set_index("template").median_corr
        best_shape = "group" if mx.get("group", -1) >= mx.get("knn_donor", -1) else "knn"
    else:
        best_shape = args.shape
    print(f"\nselected (by held-out validation): magnitude={best_mag}, shape={best_shape}")

    # ── 3. combined per-cell (deployable: selected magnitude x selected shape) ──
    m_hat_te = pd.Series(
        predict_magnitude(best_mag, struct, mag, tiers["imputable"], train_ids, te_struct, args),
        index=te_struct.substation_id)
    te_tmpl, _ = build_shape_template(best_shape, te_struct, tr_struct, tr_shape, args.k)
    imputed = im.assemble_profiles(m_hat_te, te_tmpl)
    actual = prof[prof.substation_id.isin(test_ids)][["substation_id", *CELL, "max_load", "min_load"]]
    # baseline: global mean shape x median magnitude
    global_shape = {v: tr_shape.groupby(CELL)[v].mean() for v in ("max_shape", "min_shape")}
    base_rows = []
    for _, r in te_struct.iterrows():
        for (mo, ho), sh in global_shape["max_shape"].items():
            base_rows.append((r.substation_id, mo, ho, m_base[0] * sh))
    base = pd.DataFrame(base_rows, columns=["substation_id", *CELL, "max_load"])
    pc = {}
    for value in ("max_load",):
        pc[f"imputed_{value}"] = im.percell_scores(imputed[["substation_id", *CELL, value]],
                                                   actual[["substation_id", *CELL, value]], value)
        pc[f"baseline_{value}"] = im.percell_scores(base, actual[["substation_id", *CELL, value]], value)
    pc_df = pd.DataFrame(pc).T.reset_index().rename(columns={"index": "method"})
    pc_df["skill_vs_baseline"] = 1 - pc_df.rmse / pc_df.rmse.iloc[-1]
    pc_df.to_csv(OUT_CHECKS / "percell_validation.csv", index=False)
    print("\n=== COMBINED PER-CELL (held-out max_load; skill vs naive baseline) ===")
    print(pc_df.to_string(index=False))

    _figures(mag_val, te_struct, imputed, actual, args)

    # ── 4. deploy onto unscraped targets ─────────────────────────────────────
    if not args.no_deploy:
        tgt = unscraped_structural(args.utility)
        tgt["substation_id"] = tgt.utility + "|" + tgt.substation_name
        m_dep = predict_magnitude(best_mag, struct, mag, tiers["imputable"],
                                  set(struct.substation_id), tgt, args)
        m_dep = pd.Series(np.clip(m_dep, 0, None), index=tgt.substation_id)
        dep_tmpl, dep_spread = build_shape_template(best_shape, tgt, struct, shape_long, args.k)
        out = im.assemble_profiles(m_dep, dep_tmpl)
        meta = tgt.set_index("substation_id")
        out["utility"] = out.substation_id.map(meta.utility)
        out["substation_name"] = out.substation_id.map(meta.substation_name)
        out["county"] = out.substation_id.map(meta.county_raw)
        out["lat"] = out.substation_id.map(meta.lat)
        out["lon"] = out.substation_id.map(meta.lon)
        out["donor_spread"] = out.substation_id.map(dep_spread)
        out["magnitude_method"] = best_mag
        out["shape_method"] = f"{best_shape}_k{args.k}"
        cols = ["utility", "substation_name", "county", "lat", "lon", "month", "hour",
                "min_load", "max_load", "magnitude", "donor_spread",
                "magnitude_method", "shape_method"]
        path = OUT_PROC / f"imputed_substation_profiles_{args.utility}.csv"
        out[cols].round(5).to_csv(path, index=False)
        print(f"\ndeployed: imputed {out.substation_id.nunique()} unscraped {args.utility} "
              f"substations x {out.groupby('substation_id').size().iloc[0]} cells -> "
              f"{path.relative_to(ROOT)}")


def _figures(mag_val, te_struct, imputed, actual, args) -> None:
    # ceiling bar: magnitude MAE by tier (R2 is negative/outlier-dominated across
    # the board here, so MAE is the metric that actually shows the rich-feature lift)
    fig, ax = plt.subplots(figsize=(7, 4))
    d = mag_val[mag_val.estimator == args.magnitude_model]
    ax.bar(d.tier, d.mae, color=["#4575b4", "#fee090", "#d73027"])
    ax.set_ylabel("magnitude MAE [MW] (held-out; lower=better)")
    ax.set_title(f"{args.utility}: magnitude ceiling by feature tier ({args.magnitude_model})")
    for x, v in zip(range(len(d)), d.mae):
        ax.text(x, v, f"{v:.1f}", ha="center", va="bottom", fontsize=9)
    fig.tight_layout(); fig.savefig(OUT_FIG / "ceiling_bar.png", dpi=150); plt.close(fig)

    # example held-out profiles: actual vs imputed max_load for up to 6 substations
    ids = list(te_struct.substation_id)[:6]
    fig, axes = plt.subplots(2, 3, figsize=(15, 7))
    for ax, sid in zip(axes.ravel(), ids):
        a = actual[actual.substation_id == sid].sort_values(CELL)
        p = imputed[imputed.substation_id == sid].sort_values(CELL)
        ax.plot(range(len(a)), a.max_load.to_numpy(), label="actual", lw=1)
        ax.plot(range(len(p)), p.max_load.to_numpy(), label="imputed", lw=1, alpha=0.8)
        ax.set_title(sid.split("|")[1][:22], fontsize=9)
        ax.set_xticks([])
    axes.ravel()[0].legend(fontsize=8)
    fig.suptitle(f"{args.utility}: held-out actual vs imputed max_load (288-cell, month-major)")
    fig.tight_layout(); fig.savefig(OUT_FIG / "example_profiles.png", dpi=150); plt.close(fig)


if __name__ == "__main__":
    main()
