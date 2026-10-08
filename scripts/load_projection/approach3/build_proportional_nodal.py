"""Approach 3 -- build a coordinate-free proportional nodal disaggregation.

Takes a node table (`node_id[, base_id, subname]`, `winter_load`, `summer_load`
-- dimensionless, possibly negative), a user-supplied substation->base mapping
edge list, and a statewide hourly target series, and writes hourly MW per node
with:

  * the statewide total conserved EXACTLY every hour, in float and at printed
    precision (largest-remainder apportionment), and
  * every node receiving EXACTLY its input share of each half-year's energy
    (`--conserve slack`, the default).

No coordinates, no counties, no ReEDS, no nodal map. The method, its algebra and
its feasibility theorem live in `src/load_projection/proportional.py` and
`docs/approach3_proportional.md`.

Target
------
  --target-csv PATH     generic: needs `datetime_pst` (or `dt_pst_hb`) and
                        `demand_mw`, hour-beginning fixed PST
  --target resolve      imports `build_target` / `ca_series` from
                        `deliverables/build_resolve2035_cats_package.py`, so
                        RESOLVE's Baseline-plus-overlays assembly is reused
                        rather than reimplemented. Use --year / --weather-year.

Key options
-----------
  --negative-nodes {net-base,zero,participate,refuse}
        net-base (default): a base is allocated its signed sibling NET, then
        split among its POSITIVE siblings. Every mode is just a different
        definition of l_n(b); everything downstream is mode-agnostic.
  --conserve {slack,renorm}
        slack (default) keeps seasonal energy EXACT. renorm keeps per-cell
        conservation without a slack basis but gives exactness up.
  --shape-source {envelope,stoch,fleet,flat}
        where a mapped base's hourly pattern comes from. `flat` is the
        provably-feasible baseline; `fleet` is NOT safe (same pathology).
  --shape-common {keep,strip}
        strip divides out the mapped fleet's common mode: keeps exactness and
        all cross-node shape information while shrinking slack amplification.
  --sibling-draws {off,independent}
        off (default) broadcasts one shape to all of a base's siblings, so they
        never reorder. independent gives each sibling its own eps.
  --sibling-sigma {proportional,quadrature}
        proportional (default, approved 2026-10-08): sigma_n = k_n*sigma_base
        with mu_n = k_n*mu_base. NOTE k_n cancels in the shape, so what this
        buys is the independent eps, not the sigma scaling. quadrature uses
        sqrt(k_n) so a smaller sibling is relatively noisier and k_n does not
        cancel.

Outputs (into `data/processed/load_projection/approach3/<tag>/`)
---------------------------------------------------------------
  nodal_hourly.csv.gz    datetime_pst, hour_of_year, z0001..zNNNN  (MW)
  node_index.csv         column -> node_id and every input column; the ONLY
                         link from z0001 back to reality, and the only record
                         of a zeroed node's original level
  node_energy_check.csv  per node per block: target vs realized energy share
  node_shape.csv.gz      per node per cell shape and share (--save-shapes)
  slack_diagnostics.csv  per cell: Y, M, R, lambda_M, lambda_U, shape_U
  mapping_report.csv     every edge with its status, plus per-base counts
  node_annual_peak.csv   per node annual MWh, peak MW, negative-hour count
  manifest.json          every input md5, every guard threshold AND its
                         realized value, target provenance, the run command
  summary.csv            one row of headline numbers
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/genx"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/deliverables"))

from load_projection import cells as C  # noqa: E402
from load_projection import proportional as P  # noqa: E402
from load_projection import stochastic as ST  # noqa: E402
from genx_demand_io import round_to_printed  # noqa: E402

PROCESSED = ROOT / "data/processed"
PROFILES = PROCESSED / "substations/substation_load_profiles_clean.csv"
PROJECTIONS = PROCESSED / "load_projection/projections"
OUT_PARENT = PROCESSED / "load_projection/approach3"
DEFAULT_STOCH_RUN = "stochastic__cats_caiso_target__normal__Fcal__native__calibtgt"
BLOCKS = C.HALFYEAR_LABELS


def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_rev() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------

def load_target_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    tcol = next((c for c in ("datetime_pst", "dt_pst_hb", "datetime", "timestamp")
                 if c in df.columns), None)
    if tcol is None or "demand_mw" not in df.columns:
        raise ValueError(
            f"{path}: need a timestamp column (datetime_pst / dt_pst_hb) and "
            f"demand_mw; got {list(df.columns)}")
    dt = pd.to_datetime(df[tcol])
    if not dt.is_monotonic_increasing:
        raise ValueError(f"{path}: timestamps are not sorted ascending")
    gaps = dt.diff().dropna().unique()
    if len(gaps) > 1 or (len(gaps) == 1 and gaps[0] != pd.Timedelta(hours=1)):
        raise ValueError(
            f"{path}: timestamps are not a gapless hourly series "
            f"(found spacings {list(pd.to_timedelta(gaps))[:4]}). Approach 3 "
            f"assumes hour-beginning fixed PST with no DST gaps.")
    y = pd.to_numeric(df.demand_mw, errors="coerce")
    if not np.isfinite(y).all():
        raise ValueError(f"{path}: {int((~np.isfinite(y)).sum())} non-finite "
                         f"demand_mw values")
    return pd.DataFrame({"datetime_pst": dt, "month": dt.dt.month,
                         "hour_pst": dt.dt.hour, "demand_mw": y.to_numpy()})


def load_target_resolve(args) -> tuple[pd.DataFrame, dict]:
    """Reuse the RESOLVE assembly rather than reimplementing it."""
    import build_resolve2035_cats_package as B
    target, scaling = B.build_target(args.year, B.OVERLAY_SCENARIO, args.overlays)
    wy = args.weather_year or B.median_peak_weather_year(target, args.load_basis)
    idx, y = B.ca_series(target, wy, args.load_basis)
    # ca_series returns an index FRAME (datetime_pst, hour_of_year, month,
    # hour_pst, cell) plus the MW array -- not a DatetimeIndex
    dt = pd.DatetimeIndex(idx.datetime_pst)
    comp = getattr(scaling, "attrs", {}).get("components")
    return (pd.DataFrame({"datetime_pst": dt, "month": dt.month,
                          "hour_pst": dt.hour, "demand_mw": np.asarray(y)}),
            {"source": "resolve", "model_year": int(args.year),
             "weather_year": int(wy), "load_basis": args.load_basis,
             "overlays": args.overlays, "scenario": B.OVERLAY_SCENARIO,
             "components": (comp.to_dict() if hasattr(comp, "to_dict")
                            else None)})


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------

def build_shape(args, nodes, edges, T, l, mapped, base_of_node, guards):
    """Return (shape[n_cells, n_nodes], meta)."""
    n_cells, n_nodes = C.MONTHHOUR.n_cells, len(nodes)
    meta: dict = {"shape_source": args.shape_source,
                  "shape_common": args.shape_common,
                  "sibling_draws": args.sibling_draws}
    shape = np.ones((n_cells, n_nodes))
    if args.shape_source == "flat" or not mapped.any():
        meta["note"] = "flat: M(c) == lambda_M and shape_U == 1 by construction"
        return shape, meta

    if args.shape_source == "stoch":
        run = args.stochastic_run or DEFAULT_STOCH_RUN
        src = PROJECTIONS / run / "substation_cell_mw.csv"
        if not src.exists():
            raise FileNotFoundError(
                f"{src} not found. Approach 3's stochastic shape source needs "
                f"an Approach 2 run with --save-cells.")
        arr, units, m = P.stoch_surface(src, args.draw, args.min_draws)
        meta["stochastic_run"] = run
        meta["stoch_source_md5"] = md5(src)
    else:
        arr, units, m = P.envelope_surface(PROFILES, args.shape_col)
    meta.update(m)

    if args.shape_floor is not None:
        n_clip = int((arr < args.shape_floor).sum())
        arr = np.maximum(arr, args.shape_floor)
        meta["n_clipped_before_normalization"] = n_clip

    if args.shape_source == "fleet":
        fleet = P.fleet_surface(arr, T["block_of_cell"])
        base_raw = np.repeat(fleet[:, None], int(mapped.any()) or 1, axis=1)
        bases = ["__fleet__"]
        bmeta = {"n_bases_mapped": 1}
        col_of_base = {b: 0 for b in nodes.base_id.unique()}
    else:
        base_raw, bases, bmeta = P.base_envelope(arr, units, edges)
        col_of_base = {b: i for i, b in enumerate(bases)}
    meta.update(bmeta)

    # fleet-mean fallback for a mapped base whose substations have no usable
    # envelope, so a mapped node is never left without a pattern
    fleet = P.fleet_surface(arr, T["block_of_cell"])
    base_shape, nmeta = P.energy_normalize(base_raw, T["Y"], T["block_of_cell"],
                                           guards.min_shape_net_gross)
    meta.update(nmeta)
    fleet_shape, _ = P.energy_normalize(fleet[:, None], T["Y"],
                                        T["block_of_cell"],
                                        guards.min_shape_net_gross)

    if args.sibling_draws == "independent" and args.shape_source != "fleet":
        shape, dmeta = _sibling_shape(args, nodes, edges, T, base_of_node,
                                      bases, col_of_base, mapped, guards)
        meta.update(dmeta)
    else:
        cols = np.array([col_of_base.get(b, -1) for b in nodes.base_id])
        for i in range(n_nodes):
            if not mapped[i]:
                continue
            shape[:, i] = (base_shape[:, cols[i]] if cols[i] >= 0
                           else fleet_shape[:, 0])
        meta["n_fleet_fallback_nodes"] = int(
            sum(mapped[i] and cols[i] < 0 for i in range(n_nodes)))

    if args.shape_common == "strip" and mapped.any():
        sub, smeta = P.strip_common(shape[:, mapped], l[mapped, :], T["Y"],
                                    T["block_of_cell"],
                                    guards.min_shape_net_gross)
        shape[:, mapped] = sub
        meta.update({f"strip_{k}": v for k, v in smeta.items()})
    return shape, meta


def _sibling_shape(args, nodes, edges, T, base_of_node, bases, col_of_base,
                   mapped, guards):
    """Per-sibling draws: shared z and rho(c), own eps, sigma per the mode."""
    env = ST.load_envelope_cells()
    env = env[~env.missing]
    ok = edges[edges.status == "ok"]
    use = env.merge(ok[["utility", "substation_name", "base_id"]].assign(
        utility=lambda d: d.utility.str.lower()),
        on=["utility", "substation_name"], how="inner")
    if use.empty:
        raise ValueError("--sibling-draws independent: no mapped substation has "
                         "a usable envelope, so there is no mu/sigma to draw from")
    n_cells = C.MONTHHOUR.n_cells
    mu_b = np.zeros((n_cells, len(bases)))
    sg_b = np.zeros((n_cells, len(bases)))
    for b, grp in use.groupby("base_id"):
        j = col_of_base.get(b)
        if j is None:
            continue
        agg = grp.groupby("cell")[["mu", "sigma"]].sum()
        mu_b[agg.index.to_numpy(), j] = agg.mu.to_numpy()
        # perfect correlation: the substations feeding one base are co-located
        # by construction and rho(c) already carries the common-factor
        # structure.  --base-sigma quadrature is the independent alternative.
        sg = agg.sigma.to_numpy()
        if args.base_sigma == "quadrature":
            sq = grp.groupby("cell").sigma.apply(lambda s: np.sqrt((s ** 2).sum()))
            sg = sq.to_numpy()
        sg_b[agg.index.to_numpy(), j] = sg

    tgt = pd.DataFrame({"demand_mw": T["_y"], "cell": T["cell"]})
    sys_cells, f_star = ST.build_system_cells(use, tgt)
    rho = sys_cells.rho.reindex(range(n_cells)).fillna(
        float(np.nanmedian(sys_cells.rho))).to_numpy()
    zt = tgt.copy()
    zt["dt_pst_hb"] = T["_dt"]
    z = ST.standardize_z(zt).z.to_numpy()
    z = np.where(np.isfinite(z), z, 0.0)

    # k_n = the sibling's share of its base's POSITIVE level mass, per block;
    # a single k is needed for one draw, so use the block-mean level share
    lev = np.column_stack([nodes[c].to_numpy(dtype=float)
                           for c in ("winter_load", "summer_load")])
    pos = np.maximum(lev, 0.0).mean(axis=1)
    codes = pd.factorize(nodes.base_id, sort=False)[0]
    tot = np.bincount(codes, weights=pos, minlength=codes.max() + 1)
    k = np.where(tot[codes] > 0, pos / np.where(tot[codes] > 0, tot[codes], 1.0), 0.0)

    cols = np.array([col_of_base.get(b, 0) for b in nodes.base_id])
    day = pd.factorize(pd.DatetimeIndex(T["_dt"]).normalize())[0]
    rng = np.random.default_rng(args.seed)
    realized, dmeta = P.sibling_draws(mu_b, sg_b, rho, z, T["cell"], day, k,
                                      cols, args.sibling_sigma, rng)
    shape = np.ones((n_cells, len(nodes)))
    norm, nm = P.energy_normalize(realized[:, mapped], T["Y"],
                                  T["block_of_cell"], guards.min_shape_net_gross)
    shape[:, mapped] = norm
    dmeta.update({"f_star_on_target": float(f_star), "seed": args.seed,
                  "base_sigma": args.base_sigma,
                  "rho_median": float(np.nanmedian(rho)),
                  "n_draw_columns_flat": nm["n_block_columns_flat"]})
    return shape, dmeta


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

def write_hourly(path: Path, dt, cols, S, y, cell, decimals, chunk=1000):
    n = len(y)
    acc_annual = np.zeros(S.shape[1])
    acc_peak = np.full(S.shape[1], -np.inf)
    acc_min = np.full(S.shape[1], np.inf)
    n_neg = np.zeros(S.shape[1], dtype=np.int64)
    worst = 0.0
    snap = 0.0
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", newline="", encoding="utf-8") as fh:
        fh.write("datetime_pst,hour_of_year," + ",".join(cols) + "\n")
        for a in range(0, n, chunk):
            sl = slice(a, min(a + chunk, n))
            blk = y[sl, None] * S[cell[sl], :]
            snapped = round_to_printed(blk, y[sl], decimals=decimals,
                                       allow_negative=True)
            # the invariant is against the GRID-SNAPPED target: an off-grid
            # target is unreachable by construction, so comparing to raw y
            # would always report up to half a grid step and hide a real break
            tgt = np.round(y[sl], decimals)
            worst = max(worst, float(np.abs(snapped.sum(axis=1) - tgt).max()))
            snap = max(snap, float(np.abs(tgt - y[sl]).max()))
            acc_annual += snapped.sum(axis=0)
            acc_peak = np.maximum(acc_peak, snapped.max(axis=0))
            acc_min = np.minimum(acc_min, snapped.min(axis=0))
            n_neg += (snapped < 0).sum(axis=0)
            fmt = "%." + str(decimals) + "f"
            body = pd.DataFrame(snapped).to_csv(
                header=False, index=False, float_format=fmt).rstrip("\n")
            stamps = [f"{t:%Y-%m-%d %H:%M}" for t in dt[sl]]
            hrs = range(a + 1, a + snapped.shape[0] + 1)
            for s, h, row in zip(stamps, hrs, body.split("\n")):
                fh.write(f"{s},{h},{row}\n")
    return {"annual_mwh_printed": acc_annual, "peak_mw_printed": acc_peak,
            "min_mw": acc_min, "n_hours_negative": n_neg,
            "max_hourly_conservation_error_mw": worst,
            "max_target_grid_snap_mw": snap,
            "file_mb": round(path.stat().st_size / 1e6, 2)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nodes", required=True)
    ap.add_argument("--mapping", required=True)
    ap.add_argument("--target", choices=["resolve", "csv"], default="csv")
    ap.add_argument("--target-csv")
    ap.add_argument("--year", type=int, default=2035)
    ap.add_argument("--weather-year", type=int, default=None)
    ap.add_argument("--load-basis", choices=["net", "gross"], default="net")
    ap.add_argument("--overlays", choices=["all", "none"], default="all")
    ap.add_argument("--negative-nodes", choices=list(P.NEGATIVE_NODE_MODES),
                    default="net-base")
    ap.add_argument("--on-negative-base", choices=["zero", "refuse"],
                    default="zero")
    ap.add_argument("--conserve", choices=list(P.CONSERVE_MODES), default="slack")
    ap.add_argument("--shape-source", choices=list(P.SHAPE_SOURCES),
                    default="envelope")
    ap.add_argument("--shape-col", choices=list(P.SHAPE_COLS), default="avg_load")
    ap.add_argument("--shape-common", choices=list(P.SHAPE_COMMON_MODES),
                    default="keep")
    ap.add_argument("--shape-floor", type=float, default=None)
    ap.add_argument("--sibling-draws", choices=list(P.SIBLING_DRAW_MODES),
                    default="off")
    ap.add_argument("--sibling-sigma", choices=list(P.SIGMA_MODES),
                    default="proportional")
    ap.add_argument("--base-sigma", choices=["sum", "quadrature"], default="sum")
    ap.add_argument("--draw", default="mean")
    ap.add_argument("--min-draws", type=int, default=3)
    ap.add_argument("--stochastic-run", default=None)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--decimals", type=int, default=1)
    ap.add_argument("--min-net-gross-total", type=float, default=0.10)
    ap.add_argument("--min-net-gross-unmapped", type=float, default=0.05)
    ap.add_argument("--max-slack-amplification", type=float, default=5.0)
    ap.add_argument("--min-slack-shape", type=float, default=0.0)
    ap.add_argument("--min-shape-net-gross", type=float, default=0.20)
    ap.add_argument("--min-renorm-denominator", type=float, default=0.5)
    ap.add_argument("--save-shapes", action="store_true", default=True)
    ap.add_argument("--no-save-shapes", dest="save_shapes", action="store_false")
    ap.add_argument("--tag", default=None)
    ap.add_argument("--out", default=str(OUT_PARENT))
    args = ap.parse_args()

    guards = P.Guards(args.min_net_gross_total, args.min_net_gross_unmapped,
                      args.max_slack_amplification, args.min_slack_shape,
                      args.min_shape_net_gross, args.min_renorm_denominator,
                      args.on_negative_base)

    # --- target ---
    if args.target == "resolve":
        target, tprov = load_target_resolve(args)
    else:
        if not args.target_csv:
            ap.error("--target csv needs --target-csv PATH")
        target = load_target_csv(Path(args.target_csv))
        tprov = {"source": "csv", "path": str(args.target_csv),
                 "md5": md5(Path(args.target_csv))}
    T = P.target_cell_energy(target)
    T["_y"] = target.demand_mw.to_numpy(dtype=np.float64)
    T["_dt"] = pd.DatetimeIndex(target.datetime_pst)
    y = T["_y"]
    tprov.update(twh=float(y.sum() / 1e6), peak_mw=float(y.max()),
                 n_hours=int(len(y)))
    print(f"target: {tprov['twh']:.2f} TWh, peak {tprov['peak_mw']:,.0f} MW, "
          f"{tprov['n_hours']:,} h")

    # --- nodes and levels ---
    nodes = P.read_node_table(args.nodes)
    levels, l, ldiag = P.node_level_shares(nodes, args.negative_nodes, guards)
    print(f"nodes: {ldiag['n_nodes']:,} in {ldiag['n_bases']:,} base(s); "
          f"{ldiag['n_negative_nodes']:,} carry a negative level")

    # --- mapping ---
    profiles = pd.read_csv(PROFILES, usecols=["utility", "substation_name"])
    profiles["utility"] = profiles.utility.str.lower()
    edges = P.read_mapping(args.mapping, nodes, profiles)
    n_bad = int((edges.status != "ok").sum())
    mapped_bases = set(edges.base_id[edges.status == "ok"])
    mapped = nodes.base_id.isin(mapped_bases).to_numpy()
    codes, base_ids = pd.factorize(nodes.base_id, sort=False)
    print(f"mapping: {len(edges):,} edge(s), {n_bad} unresolved; "
          f"{len(mapped_bases):,} base(s) -> {int(mapped.sum()):,} node(s) mapped")
    if n_bad:
        for st, k in edges.status[edges.status != "ok"].value_counts().items():
            print(f"    {k:>5}  {st[:100]}")

    # --- shapes ---
    shape, smeta = build_shape(args, nodes, edges, T, l, mapped, codes, guards)
    P._assert_unit_ymean(shape, T["Y"], T["block_of_cell"], "shape")

    # --- shares ---
    S, sdiag = P.node_shares(l, shape, mapped, levels, T["Y"],
                             T["block_of_cell"], args.conserve, guards)
    realized = P.realized_energy_shares(S, T["Y"], T["block_of_cell"])
    dev = np.abs(realized - l)
    if args.conserve == "slack":
        worst = float(dev.max())
        if worst > 1e-11:
            raise AssertionError(
                f"seasonal energy share deviates from the input level share by "
                f"{worst:.3e}; --conserve slack promises exactness")
        print(f"  [OK] exact seasonal energy share (max dev {worst:.2e})")
    else:
        print(f"  renorm: max seasonal energy share deviation "
              f"{dev.max() * 100:.4f} pp (NOT exact, by design)")
    for b_i, b in enumerate(BLOCKS):
        if f"A_{b}" in sdiag:
            print(f"  {b}: lambda_M={sdiag[f'lambda_M_{b}']:.4f} "
                  f"lambda_U={sdiag[f'lambda_U_{b}']:.4f} "
                  f"A={sdiag[f'A_{b}']:.3f} "
                  f"min shape_U={sdiag[f'min_shape_U_{b}']:.3f}")

    pre = P.node_annual_and_peak(S, T["Y"], T["ymax"], T["ymin"])

    # --- write ---
    tag = args.tag or (f"approach3__{args.shape_source}__{args.conserve}"
                       f"__{args.negative_nodes}"
                       + (f"__sib{args.sibling_sigma}"
                          if args.sibling_draws == "independent" else "")
                       + (f"__my{tprov.get('model_year')}"
                          f"_wy{tprov.get('weather_year')}"
                          if tprov["source"] == "resolve" else ""))
    out = Path(args.out) / tag
    out.mkdir(parents=True, exist_ok=True)
    cols, index = P.sanitize_node_ids(nodes.node_id)
    if list(index.node_id) != list(nodes.node_id):
        raise AssertionError("node index does not round-trip")

    stats = write_hourly(out / "nodal_hourly.csv.gz", T["_dt"], cols, S, y,
                         T["cell"], args.decimals)
    print(f"  [OK] hourly conservation at printed precision: "
          f"max {stats['max_hourly_conservation_error_mw']:.2e} MW "
          f"(target grid snap {stats['max_target_grid_snap_mw']:.3g} MW)")
    if stats["max_hourly_conservation_error_mw"] > 1e-6:
        raise AssertionError(
            f"hourly conservation broke at printed precision: "
            f"{stats['max_hourly_conservation_error_mw']:.3e} MW against the "
            f"grid-snapped target")

    # --- outputs ---
    n_sub = smeta.get("substations_per_base", {})
    index = index.assign(
        base_id=nodes.base_id.to_numpy(), subname=nodes.subname.to_numpy(),
        winter_load=nodes.winter_load.to_numpy(),
        summer_load=nodes.summer_load.to_numpy(),
        participating=(l != 0).any(axis=1), mapped=mapped,
        n_substations_mapped=[n_sub.get(b, 0) for b in nodes.base_id],
        shape_source=np.where(mapped, args.shape_source, "slack_residual"))
    index.to_csv(out / "node_index.csv", index=False)

    rows = []
    for b_i, b in enumerate(BLOCKS):
        rows.append(pd.DataFrame({
            "node_id": nodes.node_id, "block": b, "level": levels[:, b_i],
            "level_share_target": l[:, b_i],
            "energy_share_realized": realized[:, b_i],
            "abs_dev": dev[:, b_i]}))
    pd.concat(rows, ignore_index=True).to_csv(out / "node_energy_check.csv",
                                              index=False)

    lbl = C.label_frame(C.MONTHHOUR)
    sl = pd.DataFrame({"cell": range(C.MONTHHOUR.n_cells),
                       "month": lbl.month.to_numpy(),
                       "hour_pst": lbl.hour_pst.to_numpy(),
                       "block": [BLOCKS[b] for b in T["block_of_cell"]],
                       "target_energy_mwh": T["Y"],
                       "n_hours": T["n_hours_per_cell"]})
    if mapped.any() and args.conserve == "slack":
        M = np.zeros(C.MONTHHOUR.n_cells)
        for b_i in range(len(BLOCKS)):
            m = T["block_of_cell"] == b_i
            M[m] = shape[m, :][:, mapped] @ l[mapped, b_i]
        sl["M"] = M
        sl["R"] = 1.0 - M
        sl["lambda_U"] = [sdiag[f"lambda_U_{BLOCKS[b]}"]
                          for b in T["block_of_cell"]]
        sl["shape_U"] = sl.R / sl.lambda_U
    sl.to_csv(out / "slack_diagnostics.csv", index=False)

    rep = edges.copy()
    sib = pd.Series(1, index=nodes.base_id).groupby(level=0).size()
    part = pd.Series((l != 0).any(axis=1), index=nodes.base_id).groupby(level=0).sum()
    zmass = pd.Series(np.where((l == 0).all(axis=1), np.abs(levels).sum(axis=1), 0.0),
                      index=nodes.base_id).groupby(level=0).sum()
    rep["n_siblings"] = rep.base_id.map(sib).fillna(0).astype(int)
    rep["n_siblings_participating"] = rep.base_id.map(part).fillna(0).astype(int)
    rep["zeroed_level_mass"] = rep.base_id.map(zmass).fillna(0.0)
    rep.to_csv(out / "mapping_report.csv", index=False)

    peak_lbl = lbl.iloc[pre["peak_cell"]]
    pd.DataFrame({
        "node_id": nodes.node_id,
        "annual_mwh_float": pre["annual_mwh"],
        "annual_mwh_printed": stats["annual_mwh_printed"],
        "peak_mw_float": pre["peak_mw"],
        "peak_mw_printed": stats["peak_mw_printed"],
        "peak_month": peak_lbl.month.to_numpy(),
        "peak_hour_pst": peak_lbl.hour_pst.to_numpy(),
        "min_mw": stats["min_mw"],
        "n_hours_negative": stats["n_hours_negative"],
    }).to_csv(out / "node_annual_peak.csv", index=False)

    if args.save_shapes:
        ns = pd.DataFrame({
            "node_id": np.repeat(nodes.node_id.to_numpy(), C.MONTHHOUR.n_cells),
            "month": np.tile(lbl.month.to_numpy(), len(nodes)),
            "hour_pst": np.tile(lbl.hour_pst.to_numpy(), len(nodes)),
            "shape": shape.T.ravel(), "share": S.T.ravel()})
        ns.to_csv(out / "node_shape.csv.gz", index=False,
                  float_format="%.10g", compression="gzip")

    drift = float(np.abs(stats["annual_mwh_printed"] - pre["annual_mwh"]).max())
    bound = len(y) * 0.5 * 10 ** -args.decimals
    manifest = {
        "tag": tag, "git_rev": git_rev(),
        "command": "python " + " ".join(sys.argv[0:1] + sys.argv[1:]),
        "approach": "Approach 3 -- coordinate-free proportional disaggregation",
        "target": tprov,
        "inputs": {"nodes": str(args.nodes), "nodes_md5": md5(Path(args.nodes)),
                   "mapping": str(args.mapping),
                   "mapping_md5": md5(Path(args.mapping)),
                   "profiles_md5": md5(PROFILES)},
        "axes": {"negative_nodes": args.negative_nodes,
                 "conserve": args.conserve, "shape_source": args.shape_source,
                 "shape_col": args.shape_col, "shape_common": args.shape_common,
                 "shape_floor": args.shape_floor,
                 "sibling_draws": args.sibling_draws,
                 "sibling_sigma": args.sibling_sigma,
                 "base_sigma": args.base_sigma, "draw": args.draw,
                 "decimals": args.decimals, "seed": args.seed},
        "guards": guards.as_dict(),
        "realized": {**{k: v for k, v in ldiag.items()
                        if not isinstance(v, dict)},
                     **{k: v for k, v in sdiag.items()},
                     **{k: v for k, v in smeta.items()
                        if not isinstance(v, dict)},
                     "max_hourly_conservation_error_mw":
                         stats["max_hourly_conservation_error_mw"],
                     "max_target_grid_snap_mw":
                         stats["max_target_grid_snap_mw"],
                     "max_abs_energy_share_dev_pp": float(dev.max() * 100),
                     "max_printed_vs_float_annual_mwh": drift,
                     "printed_drift_bound_mwh": bound,
                     "n_node_hours_negative":
                         int(stats["n_hours_negative"].sum())},
        "notes": [
            "The node table's ABSOLUTE SCALE IS DISCARDED; only ratios survive "
            "and the level comes entirely from the target. Do not read the "
            "output as the source's own numbers rescaled.",
            "No coordinates, counties, ReEDS weights or nodal map are used.",
            "288 (month, hour_pst) cells is the resolution ceiling, so within a "
            "cell every node's share is constant and all intra-cell variation "
            "comes from the target series.",
            ("Siblings of a base share one shape, so they never reorder "
             "(Spearman(h10,h18) within a base is exactly 1)."
             if args.sibling_draws == "off" else
             "Per-sibling draws active: with --sibling-sigma proportional the "
             "level factor k_n CANCELS in the shape, so what this buys is the "
             "independent eps, not the sigma scaling."),
        ] + ([] if args.conserve == "slack" else [
            "conservation: per-cell renormalization; the seasonal energy share "
            "is NOT exact -- see node_energy_check.csv."]),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2,
                                                  default=str),
                                       encoding="utf-8")

    pd.DataFrame([{
        "tag": tag, "twh": tprov["twh"], "peak_mw": tprov["peak_mw"],
        "n_nodes": ldiag["n_nodes"], "n_bases": ldiag["n_bases"],
        "n_participating": int((l != 0).any(axis=1).sum()),
        "n_mapped_nodes": int(mapped.sum()),
        "n_mapped_bases": len(mapped_bases),
        "n_negative_nodes": ldiag["n_negative_nodes"],
        "n_bases_net_negative": ldiag.get("n_bases_net_negative_NovApr", 0),
        "negative_base_mass_discarded":
            ldiag.get("negative_base_mass_NovApr", 0.0),
        "n_node_hours_negative": int(stats["n_hours_negative"].sum()),
        "min_mw": float(stats["min_mw"].min()),
        "max_hourly_conservation_error_mw":
            stats["max_hourly_conservation_error_mw"],
        "max_abs_energy_share_dev_pp": float(dev.max() * 100),
        "A_NovApr": sdiag.get("A_NovApr"), "A_MayOct": sdiag.get("A_MayOct"),
        "file_mb": stats["file_mb"],
    }]).to_csv(out / "summary.csv", index=False)

    print(f"\nwrote {tag} -> {out}")
    for f in sorted(out.iterdir()):
        print(f"    {f.name:<28} {f.stat().st_size / 1e6:>8.2f} MB")


if __name__ == "__main__":
    main()
