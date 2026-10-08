"""Recompute every measured number quoted in the 2035 CATS-nodal deliverable README.

Same role as `genx/doc_numbers.py`, for the external deliverable instead of the
GenX docs: per the project rule (user, 2026-08-14) no measured figure quoted in
documentation may be hand-edited -- run this, then propagate. Every line is
labelled with the README section that quotes it, so a changed number can be
traced to the sentence it breaks.

Reads the BUILT PACKAGE (`deliverables/resolve_2035_cats_nodal/`), so it verifies
what was actually shipped, and IMPORTS the allocation functions from
`rescale_genx_demand` rather than reimplementing them, so its numbers cannot
drift from the pipeline. Build the package first:

    python scripts/load_projection/deliverables/build_resolve2035_cats_package.py

Sections
  A  header table: CA-wide net/gross energy, peak, weather year, buses, variants
  B  section 4.1 target: per-zone 2024->2035 scaling, BTM, IOU vs non-IOU split
  C  section 4.2 allocation: candidate pool, buses loaded, alpha, pool split
  D  section 4.3 county allocation: ReEDS shares and the municipal counties
  E  section 5 conservation: worst printed-precision deviation per file
  F  section 3 the three variant axes: uncovered-pool split (CATS-proportional
     vs equal), nodal map (prox vs voltres), and within-county weight source
     (envelope vs Approach 2 mean and single draws) -- each as annual/hourly
     reallocation, correlation, and per-cell share shape
  G  section 3/6 bus coverage vs CATS's own loaded set, bus-type split,
     per-bus energy distribution, peak non-coincidence
  H  section 6 caveats: weather-year peak spread, buses rounding to zero,
     ReEDS county-year sensitivity, the stochastic family's muni shortfall

CLI parameters
  --package   package root (default deliverables/resolve_2035_cats_nodal)
  --sections  comma-separated subset of A-H (default: all)

Usage
  python scripts/load_projection/deliverables/deliverable_numbers.py
  python scripts/load_projection/deliverables/deliverable_numbers.py --sections D,H
"""

from __future__ import annotations

import argparse
import json
import sys
from argparse import Namespace
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/genx"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/checks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_PKG = ROOT / "deliverables/resolve_2035_cats_nodal"
CATS_BUSES = ROOT / "data/raw/CATS/CATS_buses.csv"
CATS_DEMAND = ROOT / "data/raw/CATS/Demand_data.csv"

IOUS = ["PGE", "SCE", "SDGE"]
MUNI_COUNTIES = ["Los Angeles", "Sacramento", "Imperial", "Stanislaus"]
PRIMARY = "countyfirst_envelope_catsprop__prox"
VOLTRES = "countyfirst_envelope_catsprop__voltres"
EQUALSPLIT = "countyfirst_envelope_equalsplit__prox"
STOCH_MEAN = "countyfirst_stochmean_catsprop__prox"
D0 = "countyfirst_stochdraw0_catsprop__prox"
D1 = "countyfirst_stochdraw1_catsprop__prox"


def head(letter: str, title: str) -> None:
    print(f"\n{'=' * 72}\n{letter}. {title}\n{'=' * 72}")


def hourly(pkg: Path, variant: str) -> pd.DataFrame:
    df = pd.read_csv(pkg / "nodal_hourly" / f"{variant}.csv.gz")
    return df.filter(like="Demand_MW_z")


def variants(pkg: Path) -> list[str]:
    return [f.name[:-len(".csv.gz")] for f in sorted((pkg / "nodal_hourly").glob("*.csv.gz"))]


def load_basis(pkg: Path) -> str:
    """Which column the package actually disaggregated (net or gross)."""
    s = pd.read_csv(pkg / "summary/variant_summary.csv")
    return str(s.load_basis.iloc[0]) if "load_basis" in s.columns else "net"


def target_hourly(pkg: Path) -> np.ndarray:
    st = pd.read_csv(pkg / "reference/resolve_2035_statewide.csv")
    return st.groupby("hour_of_year")[f"{load_basis(pkg)}_mw"].sum().to_numpy()


def reallocation(x: np.ndarray, y: np.ndarray) -> float:
    return float(0.5 * np.abs(x - y).sum() / np.abs(x).sum() * 100)


# ---------------------------------------------------------------------------

def section_a(pkg: Path) -> None:
    head("A", "Header table (README top)")
    st = pd.read_csv(pkg / "reference/resolve_2035_statewide.csv",
                     parse_dates=["datetime_pst"])
    tot = st.groupby(["datetime_pst", "hour_of_year"], as_index=False)[
        ["gross_mw", "btm_mw", "net_mw"]].sum()
    basis = load_basis(pkg)
    pk = tot.loc[tot[f"{basis}_mw"].idxmax()]
    print(f"  CA-wide annual energy, net           {tot.net_mw.sum()/1e6:.2f} TWh")
    print(f"  CA-wide annual energy, gross         {tot.gross_mw.sum()/1e6:.2f} TWh")
    print(f"  BTM PV netted out                    {tot.btm_mw.sum()/1e6:.2f} TWh")
    print(f"  load basis disaggregated             {basis}")
    print(f"  CA-wide system peak ({basis})        {pk[f'{basis}_mw']:,.0f} MW")
    print(f"  peak hour_of_year                    {int(pk.hour_of_year):,}")
    print(f"  peak timestamp                       {pk.datetime_pst:%Y-%m-%d %H:%M} PST")
    print(f"  weather year                         {st.datetime_pst.dt.year.iloc[0]}")
    print(f"  hours                                {len(tot):,}")
    print(f"  RESOLVE zones covered                {st.zone.nunique()} "
          f"({', '.join(sorted(st.zone.unique()))})")
    summ = pd.read_csv(pkg / "summary/variant_summary.csv")
    for _, r in summ.iterrows():
        print(f"  {r.variant:<24} {int(r.n_buses):,} buses, {r.annual_twh:.2f} TWh, "
              f"{r.file_mb:.1f} MB")


def section_b(pkg: Path) -> None:
    head("B", "Section 4.1 -- CA-wide 2035 target construction")
    sc = pd.read_csv(pkg / "reference/resolve_2035_scaling.csv")
    print(sc.to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
    st = pd.read_csv(pkg / "reference/resolve_2035_statewide.csv")
    per = st.groupby("zone")[["gross_mw", "btm_mw", "net_mw"]].sum() / 1e6
    per["is_iou"] = per.index.isin(IOUS)
    print("\n  shipped weather-year totals (TWh):")
    print(per.round(3).to_string())
    iou = per[per.is_iou].net_mw.sum()
    non = per[~per.is_iou].net_mw.sum()
    print(f"\n  IOU net {iou:.2f} TWh + non-IOU net {non:.2f} TWh = {iou+non:.2f} TWh")
    print(f"  non-IOU share of CA-wide NET load: {non/(iou+non)*100:.1f}% "
          "(what an IOU-only package discards)")
    print(f"  gross {per.gross_mw.sum():.2f} - BTM {per.btm_mw.sum():.2f} "
          f"= net {per.net_mw.sum():.2f} TWh")


def section_c(pkg: Path) -> None:
    head("C", "Section 4.2 -- county-first allocation")
    import rescale_genx_demand as RS
    cb = RS.candidate_buses()
    print(f"  candidate bus pool (after CA county point-in-polygon)  {len(cb):,}")
    print(f"  counties represented                                  {cb.fips_int.nunique()}")
    det = pd.read_csv(pkg / "summary/county_allocation.csv")
    print(f"  counties in the allocation                            {len(det)}")
    print(f"  counties with NO substation-covered bus (alpha=1)     "
          f"{int((det.n_substation_nodes == 0).sum())}")
    print(f"  counties with no uncovered bus (alpha=0)              "
          f"{int((det.n_uncovered_nodes == 0).sum())}")
    print(f"  alpha = u/n: min {det.alpha.min():.3f}  median {det.alpha.median():.3f}  "
          f"max {det.alpha.max():.3f}")
    print(f"  equal-pool share of the state     {det.equal_pool_share.sum():.4f}")
    print(f"  envelope-pool share of the state  {det.envelope_pool_share.sum():.4f}")
    print(f"  total buses: substation-covered {int(det.n_substation_nodes.sum()):,}  "
          f"uncovered {int(det.n_uncovered_nodes.sum()):,}")
    summ = pd.read_csv(pkg / "summary/variant_summary.csv")
    for _, r in summ.iterrows():
        print(f"  {r.variant:<24} buses with load {int(r.n_buses):,}   "
              f"negative bus-hours {int(r.n_negative_cells)}   min {r.min_mw:.1f} MW")


def section_d(pkg: Path) -> None:
    head("D", "Section 4.3 -- county allocation, especially the municipal counties")
    det = pd.read_csv(pkg / "summary/county_allocation.csv")
    a = pd.read_csv(pkg / "summary/node_annual_mwh.csv")
    m = pd.read_csv(pkg / "reference/cats_node_metadata.csv")[["bus_i", "county_name", "Type"]]
    j = a.merge(m, on="bus_i")
    tot = j[PRIMARY].sum()
    g = j.groupby("county_name")[PRIMARY].agg(twh=lambda s: s.sum() / 1e6, n_buses="size")
    g["pct_of_state"] = j.groupby("county_name")[PRIMARY].sum() / tot * 100
    g["reeds_share_pct"] = det.set_index("county_name").county_share * 100
    print(f"  CA-wide allocated {tot/1e6:.2f} TWh across {len(j):,} buses")
    print("\n  top 8 counties:")
    print(g.sort_values("twh", ascending=False).head(8).round(3).to_string())
    print("\n  the counties an IOU-only package leaves empty:")
    print(g.loc[MUNI_COUNTIES].round(3).to_string())
    print("\n  ACCEPTANCE TEST -- allocated county share vs ReEDS county share,")
    print("  max abs deviation over all 57 counties, for EVERY variant:")
    ref = det.set_index("county_name").county_share * 100
    for v in [c for c in a.columns if c != "bus_i"]:
        got = j.groupby("county_name")[v].sum()
        got = got / got.sum() * 100
        dev = (got - ref).abs()
        print(f"    {v:<42} {dev.max():.6f} pp  (worst: {dev.idxmax()})")
    print("  county totals are exact by construction and invariant to both axes")
    for c in MUNI_COUNTIES:
        cells = "  ".join(
            f"{v.replace('countyfirst_','').split('__')[0]:<17}"
            f"{j.groupby('county_name')[v].sum()[c]/j[v].sum()*100:7.3f}%"
            for v in [PRIMARY, STOCH_MEAN] if v in a.columns)
        print(f"    {c:<14} ReEDS {ref[c]:7.3f}%   {cells}")


def section_e(pkg: Path) -> None:
    head("E", "Section 5 -- hourly conservation of the shipped files")
    tgt = target_hourly(pkg)
    grid = 0.1
    worst = 0.0
    for v in variants(pkg):
        s = hourly(pkg, v).sum(axis=1).to_numpy()
        e = float(np.abs(s - tgt).max())
        worst = max(worst, e)
        print(f"  {v:<26} max |row sum - CA-wide target| {e:.4f} MW")
    print(f"\n  worst across all files: {worst:.4f} MW "
          f"(half the {grid} MW print grid = {grid/2}; "
          f"{worst/tgt.max()*1e6:.2f} ppm of peak)")


def section_f(pkg: Path) -> None:
    head("F", "Section 3 -- the three variant axes")
    a = pd.read_csv(pkg / "summary/node_annual_mwh.csv", index_col="bus_i")
    have = set(a.columns)
    pairs = [
        ("uncovered split: cats vs equal", PRIMARY, EQUALSPLIT),
        ("nodal map: prox vs voltres", PRIMARY, VOLTRES),
        ("weights: envelope vs stoch mean", PRIMARY, STOCH_MEAN),
        ("weights: envelope vs stoch draw0", PRIMARY, D0),
        ("stoch draw0 vs draw1", D0, D1),
        ("stoch mean vs draw0", STOCH_MEAN, D0),
    ]
    print(f"  {'comparison':<36} {'annual%':>8} {'hourly%':>8} "
          f"{'pearson':>9} {'spearman':>9}")
    for label, x, y in pairs:
        if x not in have or y not in have:
            continue
        xa, ya = a[x].fillna(0).to_numpy(), a[y].fillna(0).to_numpy()
        hx, hy = hourly(pkg, x), hourly(pkg, y)
        cols = hx.columns.union(hy.columns)
        A = hx.reindex(columns=cols, fill_value=0).to_numpy()
        B = hy.reindex(columns=cols, fill_value=0).to_numpy()
        hr = float(np.mean(0.5 * np.abs(A - B).sum(axis=1) / A.sum(axis=1)) * 100)
        pe = a[[x, y]].dropna().corr().iloc[0, 1]
        sp = a[[x, y]].dropna().corr(method="spearman").iloc[0, 1]
        print(f"  {label:<36} {reallocation(xa, ya):>7.2f}% {hr:>7.2f}% "
              f"{pe:>9.4f} {sp:>9.4f}")
    print()
    print("  Read: the UNCOVERED-POOL SPLIT is by far the largest axis -- it governs")
    print("  ~51% of CA-wide load. The weight source barely moves annual energy, so")
    print("  envelope and stochastic are NOT independent estimates; their visible")
    print("  signal is per-cell shape and draw-to-draw spread.")
    print()
    print("  within-year variability: CV of a bus's hourly share of CA-wide load,")
    print("  split by pool (equal-pool buses are flat by construction)")
    import rescale_genx_demand as RS
    per_node, _ = RS.envelope_node_weights(Namespace(map="prox", system="CATS"), {})
    for v in [PRIMARY, EQUALSPLIT, STOCH_MEAN, D0]:
        if v not in have:
            continue
        ref = hourly(pkg, v)
        h = ref.to_numpy()
        sh = h / h.sum(axis=1, keepdims=True)
        ids = [c[len("Demand_MW_z"):] for c in ref.columns]
        cov = np.array([per_node.get(b, 0.0) > 0 for b in ids])
        with np.errstate(invalid="ignore", divide="ignore"):
            cv = sh.std(0) / sh.mean(0)
        env = cv[cov & np.isfinite(cv)]
        eq = cv[(~cov) & np.isfinite(cv)]
        print(f"    {v:<42} envelope pool {np.median(env):.4f}  "
              f"equal pool {np.median(eq):.4f}")


def section_f2(pkg: Path) -> None:
    """Appendix to F: how uneven the CATS-proportional uncovered split really is."""
    import rescale_genx_demand as RS
    cats = RS.cats_bus_demand()
    cb = RS.candidate_buses().set_index("node").county_name
    a = pd.read_csv(pkg / "summary/node_annual_mwh.csv", index_col="bus_i")
    per_node, _ = RS.envelope_node_weights(Namespace(map="prox", system="CATS"), {})
    ids = [str(b) for b in a.index]
    unc = np.array([per_node.get(b, 0.0) <= 0 for b in ids])
    print()
    print("  the uncovered pool (buses with no metered substation):")
    for v in [PRIMARY, EQUALSPLIT]:
        if v not in a.columns:
            continue
        sh = a[v].to_numpy()
        print(f"    {v:<44} {int(unc.sum()):,} buses carry "
              f"{sh[unc].sum()/sh.sum()*100:.1f}% of CA-wide load")
    cty = pd.Series([cb.get(b) for b in ids], index=a.index)
    d = pd.DataFrame({"cats": [float(cats.get(b, 0.0)) for b in ids],
                      "county": cty})[unc]
    cv = d.groupby("county").cats.agg(
        lambda x: x.std() / x.mean() if len(x) > 1 and x.mean() > 0 else np.nan).dropna()
    print(f"    CATS's own MWh across a county's uncovered buses -- CV: "
          f"median {cv.median():.2f}  p90 {cv.quantile(.9):.2f}  max {cv.max():.2f}")
    print("    (CV 0 would mean CATS also treats them as interchangeable)")
    for c in ("Sacramento", "Imperial"):
        sel = (cty == c).to_numpy() & unc
        for v in [PRIMARY, EQUALSPLIT]:
            if v not in a.columns:
                continue
            x = a[v].to_numpy()[sel]
            tag = "catsprop " if v == PRIMARY else "equalsplit"
            print(f"    {c:<12} {tag} per-bus GWh: min {x.min()/1e3:8.2f}  "
                  f"max {x.max()/1e3:8.2f}  ratio {x.max()/max(x.min(), 1e-9):7.1f}x")


def section_g(pkg: Path) -> None:
    head("G", "Sections 3 and 6 -- bus coverage against CATS's own loaded set")
    dem = pd.read_csv(CATS_DEMAND)
    cats = pd.Series({int(c[len("Demand_MW_z"):]):
                      float(np.nansum(pd.to_numeric(dem[c], errors="coerce")))
                      for c in dem.columns if c.startswith("Demand_MW_z")})
    loaded = cats[cats.abs() > 0]
    a = pd.read_csv(pkg / "summary/node_annual_mwh.csv", index_col="bus_i")
    import rescale_genx_demand as RS
    ctrl = RS.cats_loaded_buses()
    print(f"  buses CATS loads in its own Demand_data.csv   {len(loaded):,}")
    print(f"  buses CATS loads in the GenX control tree      {len(ctrl):,}  "
          "(the set the pool restriction uses)")
    for mp in ("prox", "voltres", "nameprox"):
        try:
            nm = RS.name_assigned_nodes(Namespace(map=mp, system="CATS"))
        except FileNotFoundError:
            continue
        print(f"    map {mp:<9} direct name-matched buses {len(nm):>5}, "
              f"of which CATS-unloaded {len(nm - ctrl):>5}  "
              "(the only permitted exception)")
    for v in [c for c in a.columns if c.startswith("countyfirst")]:
        got = set(a[v].dropna()[a[v].fillna(0) > 0].index)
        miss = loaded[~loaded.index.isin(got)]
        print(f"\n  {v}: {len(got):,} buses carry load")
        print(f"    CATS-loaded buses left at zero  {len(miss):,} "
              f"({len(miss)/len(loaded)*100:.1f}%), holding "
              f"{miss.sum()/loaded.sum()*100:.1f}% of CATS's own demand")
        print(f"    buses loaded here that CATS does NOT load  {len(got - set(loaded.index)):,}")
    m = pd.read_csv(pkg / "reference/cats_node_metadata.csv")[["bus_i", "Type", "kV"]]
    j = a.reset_index().merge(m, on="bus_i")
    print("\n  allocated load by CATS bus type:")
    print(j.groupby("Type")[PRIMARY].agg(n_buses="size",
                                         twh=lambda s: s.sum() / 1e6).round(3).to_string())
    print("\n  allocated load by voltage class:")
    print(j.groupby("kV")[PRIMARY].agg(n_buses="size",
                                       twh=lambda s: s.sum() / 1e6).round(2).to_string())
    e = j[PRIMARY] / 1e3
    print(f"\n  per-bus annual energy: median {e.median():,.1f} GWh  "
          f"p90 {e.quantile(.9):,.1f} GWh  max {e.max():,.1f} GWh")
    p = pd.read_csv(pkg / "summary/node_peak_mw.csv")[PRIMARY]
    sysp = hourly(pkg, PRIMARY).sum(axis=1).max()
    print(f"  sum of per-bus peaks {p.sum():,.0f} MW vs CA-wide peak {sysp:,.0f} MW "
          f"(ratio {p.sum()/sysp:.2f}) -- bus peaks are non-coincident")


def section_h(pkg: Path) -> None:
    head("H", "Section 6 -- caveats")
    import build_resolve2035_cats_package as B
    target, _ = B.build_target()
    pk = (target.groupby(["weather_year", "datetime_pst"]).net_mw.sum()
          .groupby("weather_year").max())
    st = pd.read_csv(pkg / "reference/resolve_2035_statewide.csv",
                     parse_dates=["datetime_pst"])
    wy = int(st.datetime_pst.dt.year.iloc[0])
    print(f"  CA-wide net 2035 annual peak across RESOLVE's {len(pk)} weather years:")
    print(f"    min    {pk.min():,.0f} MW (wy {pk.idxmin()})  "
          f"{(pk.min()/pk[wy]-1)*100:+.1f}% vs shipped")
    print(f"    median {pk.median():,.0f} MW")
    print(f"    max    {pk.max():,.0f} MW (wy {pk.idxmax()})  "
          f"{(pk.max()/pk[wy]-1)*100:+.1f}% vs shipped")
    print(f"    shipped (wy {wy}) {pk[wy]:,.0f} MW")

    a = pd.read_csv(pkg / "summary/node_annual_mwh.csv")
    for v in [c for c in a.columns if c.startswith("countyfirst")]:
        z = int((a[v].fillna(0) <= 0).sum())
        print(f"\n  {v}: buses whose share rounds to 0.0 MW in every hour: {z} "
              f"of {len(a):,}")

    print("\n  ReEDS county-year sensitivity (NORMALIZED shares only):")
    import rescale_genx_demand as RS
    from validate_county_reeds import reeds_county_annual
    r = reeds_county_annual()
    yrs = sorted(r.year.unique())
    base = r[r.year == 2023].set_index("county_name").reeds_mwh
    base = base / base.sum()
    for yr in [yrs[0], 2019, yrs[-1]]:
        o = r[r.year == yr].set_index("county_name").reeds_mwh
        o = o / o.sum()
        j = pd.concat([base, o], axis=1, keys=["b", "o"]).dropna()
        print(f"    {yr} vs 2023 (shipped): reallocation "
              f"{reallocation(j.b.to_numpy(), j.o.to_numpy()):.2f}% of state load")

    print()
    print("  the stochastic POOL family standalone -- why it is not shipped, and")
    print("  why the failure is GATE-DEPENDENT rather than unconditional:")
    import rescale_genx_demand as RS
    det = pd.read_csv(pkg / "summary/county_allocation.csv").set_index("county_name")
    cb = RS.candidate_buses()
    cty = cb.set_index("node").county_name
    RUN = "stochastic__cats_caiso_target__normal__Fcal__native__calibtgt"
    cache: dict = {}
    ref = det.county_share * 100
    print(f"    {'gate':>5} {'gated':>6} {'buses':>7} {'beta':>7}  "
          + "  ".join(f"{c[:10]:>11}" for c in MUNI_COUNTIES) + "   maxdev")
    print(f"    {'ReEDS':>5} {'':>6} {'':>7} {'':>7}  "
          + "  ".join(f"{ref[c]:>10.3f}%" for c in MUNI_COUNTIES))
    for gate in (2.0, 0.30, 0.05, 0.0):
        args = Namespace(map="prox", system="CATS", weights="stoch", level="monthhour",
                         stoch_gate=gate, stoch_topoff="equal", draw="mean",
                         min_draws=3, stochastic_run=RUN, year=2019)
        try:
            sh, _, meta = RS.stoch_pool_shares(args, cache)
        except Exception as exc:                                  # noqa: BLE001
            print(f"    gate {gate}: could not evaluate ({exc})")
            continue
        v = sh.groupby("node").share.sum()
        v = v[v > 0]
        per = v.groupby(cty.reindex(v.index)).sum() * 100
        dev = (per.reindex(ref.index).fillna(0) - ref).abs()
        print(f"    {gate:>5} {meta['n_counties_gated']:>6} {len(v):>7,} "
              f"{meta['beta_equal_pool']:>7.4f}  "
              + "  ".join(f"{per.get(c, 0.0):>10.3f}%" for c in MUNI_COUNTIES)
              + f"   {dev.max():.2f}pp")
    print("    gate 0.0 DOES load all 3,769 buses, so the coverage failure is")
    print("    gate-dependent, not unconditional -- but beta/n_unc is a single")
    print("    STATEWIDE constant with no county grouping, so it distributes by BUS")
    print("    COUNT: Sacramento overshoots, Los Angeles comes in light. That is why")
    print("    the pool family is not shipped and the WEIGHT SOURCE is used instead.")


def _f(pkg: Path) -> None:
    section_f(pkg)
    section_f2(pkg)


# Allocation config section I recomputes against. The defaults ARE the
# published package's settings, so the documented figures are reproduced by a
# bare run; main() overrides them from the CLI for a pre-flight check of a run
# that uses a different namespace or bus-type restriction.
GOV_CFG = {"system": "CATS", "pool": "cats_loaded", "uncovered": "cats",
           "bus_types": "all"}


def section_i(pkg: Path) -> None:
    """External seasonal load weight source: resolution, shape, coverage gain."""
    head("I", "External seasonal load weight source (docs/external_loads.md)")
    import rescale_genx_demand as RS
    from load_projection import cells as C

    ext_root = ROOT / "data/processed/load_projection/external_loads"
    tags = sorted(d.name for d in ext_root.glob("*")
                  if (d / "external_substation_weights.csv").exists()) if ext_root.exists() else []
    if not tags:
        print("  SKIPPED: no external weight folders built. See "
              "scripts/load_projection/external_loads/build_external_weights.py")
        return

    half = C.get_spec("halfyear")
    lbl = C.label_frame(half).iloc[:, 0].to_numpy()

    print(f"  reporting every folder present locally ({len(tags)}); fixture and")
    print("  real-input folders look alike here, so check each manifest's input_file")
    print("  resolution and shape, per built folder:")
    for t in tags:
        d = ext_root / t
        rep = pd.read_csv(d / "resolution_report.csv", dtype={"node": str})
        sub = pd.read_csv(d / "external_substation_weights.csv")
        man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        sub["block"] = lbl[C.encode(half, sub.month)]
        cv = sub.groupby(["utility", "substation_name", "block"]).weight.agg(
            lambda x: 0.0 if x.mean() == 0 else x.std() / x.mean())
        routes = rep.route.value_counts().to_dict()
        shape_desc = man["shape_mode"] + f" ({man['shape_col']})"
        if man.get("shape_file"):
            shape_desc += f" + {man['shape_file']}"
        print(f"    {t}")
        print(f"      rows {len(rep):,}   " + "  ".join(
            f"{k} {v:,}" for k, v in sorted(routes.items())))
        print(f"      units {man.get('units', 'mw')} in column "
              f"{man.get('weight_column', 'weight')!r}; "
              f"{man.get('n_rows_with_coordinates', 0):,} rows carried coordinates")
        print(f"      shape {shape_desc}, within-block CV median {cv.median():.6f}")
        print(f"      refused: {len(man['ambiguous_refused']['profiled_names_ambiguous_across_utilities'])} "
              f"cross-utility names, "
              f"{len(man['ambiguous_refused']['dictionary_inversions_ambiguous'])} dictionary inversions")
        noted = rep[(rep.route != "unresolved")
                    & rep.status.astype(str).str.contains(";", na=False)]
        if len(noted):
            print(f"      resolved with a note (proximity or demotion): {len(noted):,}")
        nd = man.get("name_match_distance_km")
        if nd:
            print(f"      name-vs-coordinate distance: median {nd['50%']} km, "
                  f"p90 {nd.get('90%')} km, max {nd['max']} km")
        dups = man.get("duplicate_targets") or []
        if dups:
            print(f"      targets receiving >1 input row (weights SUMMED): {len(dups)}")
        n_fb = man["counts"].get("n_route1_cells_shape_from_fallback", 0)
        print(f"      named-route cells needing a borrowed shape: {n_fb:,}")

    print("\n  measurement-driven share of load -- NOTE 100% of load is always")
    print("  allocated; this is the share whose WITHIN-COUNTY split is measured:")
    dflt = {"system": "CATS", "pool": "cats_loaded", "uncovered": "cats",
            "bus_types": "all"}
    if GOV_CFG != dflt:
        print(f"    [config: {', '.join(f'{k}={v}' for k, v in GOV_CFG.items())}]")
        print("    NOT the published configuration -- these figures do not "
              "belong in the docs")

    def gov(src, ext=None):
        a = Namespace(map="prox", alpha="ratio", county_year=2023,
                      county_weights=src, weights="reedsco",
                      external_loads=ext, draw="mean",
                      min_draws=3, stochastic_run="", year=None, **GOV_CFG)
        shares, detail, meta = RS.county_first_shares(a, {})
        return meta["envelope_governed_share"], int(detail.n_substation_nodes.sum()), detail

    g_env, n_env, d_env = gov("envelope")
    print(f"    {'envelope (published default)':34s} {g_env * 100:6.2f}%   "
          f"covered buses {n_env:,}")
    for t in tags:
        g, n, d = gov("external", t)
        print(f"    {t:34s} {g * 100:6.2f}%   covered buses {n:,}   "
              f"({(g - g_env) * 100:+.2f} pp vs envelope)")
        j = d_env.merge(d, on=["fips_int", "county_name"], suffixes=("_env", "_ext"))
        j["gain"] = (j.county_share_ext * (1 - j.alpha_ext)
                     - j.county_share_env * (1 - j.alpha_env))
        top = j.sort_values("gain", ascending=False).head(4)
        for _, r in top.iterrows():
            print(f"        {r.county_name:16s} covered "
                  f"{int(r.n_substation_nodes_env):4d} -> {int(r.n_substation_nodes_ext):4d}   "
                  f"alpha {r.alpha_env:.4f} -> {r.alpha_ext:.4f}")


def section_j(pkg: Path) -> None:
    """A compact grid package: combinations, sizes, and the weather-year spread."""
    head("J", "Compact grid package (docs/deliverables.md)")
    shares_dir, sw_dir = pkg / "shares", pkg / "statewide"
    if not shares_dir.exists() or not sw_dir.exists():
        print(f"  SKIPPED: {pkg.name} is not a compact package "
              f"(no shares/ + statewide/). Build one with --format compact.")
        return

    sh = sorted(shares_dir.glob("*.npz"))
    sw = sorted(sw_dir.glob("*.csv*"))
    sh_mb = sum(p.stat().st_size for p in sh) / 1e6
    sw_mb = sum(p.stat().st_size for p in sw) / 1e6
    print(f"  {len(sh)} variant(s) x {len(sw)} series = "
          f"{len(sh) * len(sw)} combination(s)")
    print(f"  shares     {sh_mb:7.2f} MB  ({sh_mb / max(len(sh), 1):.2f} MB each)")
    print(f"  statewide  {sw_mb:7.2f} MB  ({sw_mb / max(len(sw), 1):.3f} MB each)")
    print(f"  total      {sh_mb + sw_mb:7.2f} MB")
    # what the same grid would cost expanded, from the precedent's measured size
    print(f"  the same grid as hourly CSVs would be ~"
          f"{len(sh) * len(sw) * 25.1 / 1000:.1f} GB at the precedent's 25.1 MB "
          f"per equalsplit file")

    print("\n  share matrices (rows must sum to 1):")
    for p in sh:
        z = np.load(p, allow_pickle=False)
        S = z["shares"]
        dev = float(np.abs(S.sum(axis=1) - 1.0).max())
        neg = int((S < 0).sum())
        print(f"    {p.stem:<52} {S.shape[0]} x {S.shape[1]:,}  "
              f"row-sum dev {dev:.2e}  negatives {neg}")
        if dev > 1e-9 or neg:
            print(f"      !! this matrix is not a valid allocation")

    print("\n  statewide series:")
    rows = []
    for p in sw:
        d = pd.read_csv(p)
        label = p.name.split(".csv")[0]
        rows.append({"series": label, "hours": len(d),
                     "twh": d.y_mw.sum() / 1e6, "peak_mw": d.y_mw.max()})
    t = pd.DataFrame(rows)
    t["model_year"] = t.series.str.extract(r"y(\d{4})")[0]
    t["weather_year"] = t.series.str.extract(r"wy(\d{4})")[0]
    print(t[["series", "hours", "twh", "peak_mw"]].to_string(
        index=False, float_format=lambda v: f"{v:,.2f}"))

    if t.weather_year.notna().all() and t.model_year.nunique() >= 1:
        print("\n  weather-year spread per model year -- RESOLVE's OVERLAYS CARRY NO")
        print("  WEATHER DIMENSION, so only the Baseline varies and the spread is")
        print("  narrower than a Baseline-only view would suggest:")
        for my, g in t.groupby("model_year"):
            if len(g) < 2:
                continue
            pk, tw_ = g.peak_mw, g.twh
            print(f"    {my}: peak {pk.min():,.0f}-{pk.max():,.0f} MW "
                  f"({(pk.max()/pk.min()-1)*100:+.1f}% high vs low, "
                  f"median {pk.median():,.0f}); energy "
                  f"{tw_.min():.2f}-{tw_.max():.2f} TWh "
                  f"({(tw_.max()/tw_.min()-1)*100:+.2f}%)")
            print(f"         high wy {g.loc[pk.idxmax(), 'weather_year']}, "
                  f"low wy {g.loc[pk.idxmin(), 'weather_year']}")

    exp = pkg / "code" / "expand.py"
    print(f"\n  bundled expander: {'present' if exp.exists() else 'MISSING'}"
          f"{'' if exp.exists() else ' -- the package cannot be used as shipped'}")


#: sections that recompute from the repo and need no built package
PACKAGE_FREE_SECTIONS = {"I"}

SECTIONS = {"I": section_i, "J": section_j,
            "A": section_a, "B": section_b, "C": section_c, "D": section_d,
            "E": section_e, "F": _f, "G": section_g, "H": section_h}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--package", default=str(DEFAULT_PKG))
    ap.add_argument("--sections", default=",".join(SECTIONS))
    # section I only: recompute against a different allocation config. Defaults
    # are the published package's, so omitting these reproduces the docs.
    ap.add_argument("--system", default="CATS",
                    help="nodal namespace for section I's recompute")
    ap.add_argument("--bus-types", choices=["all", "substation"], default="all",
                    help="section I: 'substation' excludes every CATS AddedNode")
    ap.add_argument("--uncovered", choices=["cats", "equal"], default="cats",
                    help="section I: how a county's uncovered pool splits")
    ap.add_argument("--pool", choices=["cats_loaded", "all"], default="cats_loaded",
                    help="section I: which buses are eligible at all")
    args = ap.parse_args()
    GOV_CFG.update(system=args.system, pool=args.pool,
                   uncovered=args.uncovered, bus_types=args.bus_types)
    pkg = Path(args.package)
    wanted = [x.strip().upper() for x in args.sections.split(",") if x.strip()]
    # section I recomputes from the repo, not from a built package, so it stays
    # runnable before a build; every other section needs the package
    if not (pkg / "summary/variant_summary.csv").exists():
        if set(wanted) - PACKAGE_FREE_SECTIONS:
            raise SystemExit(
                f"no built package at {pkg} -- run the build script first")
        print(f"(no built package at {pkg}; package-free sections only)")
    for s in wanted:
        SECTIONS[s](pkg)
    print(f"\nPackage: {pkg}")


if __name__ == "__main__":
    main()
