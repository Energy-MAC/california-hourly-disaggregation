"""Recompute every number quoted in the GenX docs, so each figure is auditable.

The GenX documentation (docs/genx_rescale.md, docs/genx_runbook.md,
docs/genx_comparison.md, the README GenX sections, CLAUDE.md) quotes measured
numbers: pool sizes, fallback counts, F*, identity-match rates, relocation
percentages.  Prose goes stale silently; this script recomputes each of them
from primary sources and prints them labeled with WHERE they are quoted, so a
doc edit can be checked against a fresh run and updated numbers can be pasted
in with provenance.

Each section is a function; comment one out in main() if its inputs are absent.
Sections that depend on generated artifacts (comparison summary, map summary)
read those artifacts and say so -- re-run the generating script first if stale:

  A. control tree          genx/ scenario files themselves
  B. candidate bus pool    CATS_buses.csv + control demand support
  C. rep-week calendar     genx/rep_week_calendar.csv
  D. envelope cell fallback substation_load_profiles_clean.csv x calendar cells
  E. stochastic run        the Approach 2 CATS-calibrated run (F* recomputed
                           from first principles: envelopes + CATS target)
  F. nodal maps            the four map artifacts + LP stats
  G. rescale runs          every genx/rescaled manifest (conservation, pools)
  H. input divergence      data/checks/genx_rescale/demand_comparison_summary.csv

Usage
  python scripts/load_projection/genx/doc_numbers.py
  python scripts/load_projection/genx/doc_numbers.py --sections A,B,E
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/load_projection/genx"))
sys.path.insert(0, str(ROOT / "src"))
from genx_demand_io import (  # noqa: E402
    load_rep_week_calendar, read_demand, scenario_seasons)

PROCESSED = ROOT / "data/processed"
STOCH_RUN = (PROCESSED / "load_projection/projections/"
             "stochastic__cats_caiso_target__normal__Fcal__native__calibtgt")
CATS_TARGET = PROCESSED / "load_projection/cats_caiso_target.csv"
NODAL = PROCESSED / "load_projection/nodal/CATS"
RESCALED = ROOT / "genx/rescaled"
CHECKS = ROOT / "data/checks"


def hdr(tag: str, title: str) -> None:
    print(f"\n=== [{tag}] {title} " + "=" * max(0, 60 - len(title)))


def section_a_control() -> None:
    hdr("A", "Control tree (quoted in genx_rescale.md 'The control set')")
    tree = scenario_seasons()
    print(f"cases: {len(tree.cases)}   seasons: {len(tree.seasons)} "
          f"({', '.join(tree.seasons)})   distinct demand files: "
          f"{len(set(tree.season_md5.values()))}")
    shares = {}
    for season, case in tree.canonical.items():
        d = read_demand(tree.demand_path(case))
        tot = d.values.sum()
        by_bus = d.values.sum(axis=0)
        shares[season] = by_bus / tot
        print(f"  {season:7s} zones {len(d.zones):,}  nonzero "
              f"{(by_bus > 0).sum():,}  week total {tot/1e6:.3f} TWh  "
              f"hours {d.n_hours}")
    # season-invariance of the control's spatial allocation
    mats = np.vstack(list(shares.values()))
    reloc = 0.5 * np.abs(mats - mats.mean(axis=0)).sum(axis=1)
    print(f"control allocation season-invariance: per-bus share max deviation "
          f"{np.abs(mats - mats.mean(axis=0)).max():.2e}; "
          f"max seasonal relocation vs mean {100*reloc.max():.3f}%")


def section_b_pool() -> None:
    hdr("B", "Candidate bus pool (genx_rescale.md 'Which buses are eligible')")
    from rescale_genx_demand import candidate_buses
    nodes = pd.read_csv(ROOT / "data/raw/CATS/CATS_buses.csv")
    for c in nodes.columns:
        if pd.api.types.is_string_dtype(nodes[c]):
            nodes[c] = nodes[c].str.strip().str.strip("'").str.strip()
    tree = scenario_seasons()
    loaded, load_by_zone = set(), {}
    for season, case in tree.canonical.items():
        d = read_demand(tree.demand_path(case))
        for z, tot in zip(d.zones, d.values.sum(axis=0)):
            load_by_zone[z] = load_by_zone.get(z, 0.0) + tot
            if tot > 0:
                loaded.add(z)
    ids = nodes.bus_i.astype(str)
    n_sub = int(((nodes.Type == "Substation") & (nodes.Import != "IMPORT")).sum())
    added_loaded = ((nodes.Type == "AddedNode") & ids.isin(loaded))
    n_added = int(added_loaded.sum())
    n_added_zero = int(((nodes.Type == "AddedNode") & ~ids.isin(loaded)).sum())
    added_share = sum(load_by_zone.get(z, 0.0) for z in ids[added_loaded]) \
        / sum(load_by_zone.values())
    print(f"Type=Substation non-IMPORT: {n_sub:,}")
    print(f"AddedNodes CATS loads (kept): {n_added:,}  carrying "
          f"{100*added_share:.1f}% of state load")
    print(f"AddedNodes at zero (excluded): {n_added_zero:,}")
    print(f"pool: {n_sub + n_added:,}; with a county polygon: "
          f"{len(candidate_buses()):,} (the difference falls outside every "
          f"county polygon)")
    print(f"control loads {len(loaded):,} buses")


def section_c_calendar() -> None:
    hdr("C", "Rep-week calendar (genx_rescale.md 'Rep-week calendar')")
    cal = load_rep_week_calendar()
    raw = pd.read_csv(ROOT / "genx/rep_week_calendar.csv")
    print(raw.to_string(index=False))
    all_cells = set()
    for season, cells in cal.items():
        cs = set(cells)
        all_cells |= cs
        print(f"  {season:7s} {len(cs)} distinct (month,hour) cells; "
              f"first cell {cells[0]}")
    print(f"union across the four weeks: {len(all_cells)} of 288 cells")


def section_d_envelope_fallback() -> None:
    hdr("D", "Envelope month-hour fallback (genx_rescale.md fallback table)")
    from load_projection.weights import load_profiles
    prof = load_profiles(
        PROCESSED / "substations/substation_load_profiles_clean.csv", "max_load")
    prof = prof.groupby(["utility", "substation_name", "month", "hour_pst"],
                        as_index=False)["max_load"].mean()
    have = set(map(tuple, prof[["utility", "substation_name", "month",
                                "hour_pst"]].itertuples(index=False)))
    subs = prof[["utility", "substation_name"]].drop_duplicates()
    cal = load_rep_week_calendar()
    total_missing = 0
    for scope, cells in [*[(s, sorted(set(c))) for s, c in cal.items()],
                         ("all 288 cells", [(m, h) for m in range(1, 13)
                                            for h in range(24)])]:
        n_slots = len(subs) * len(cells)
        n_miss = sum((u, s, m, h) not in have
                     for u, s in subs.itertuples(index=False)
                     for (m, h) in cells)
        print(f"  {scope:14s} {len(cells):3d} cells  {n_slots:,} slots  "
              f"{n_miss} missing ({100*n_miss/n_slots:.2f}%)")
        if scope != "all 288 cells":
            total_missing += n_miss


def section_e_stochastic() -> None:
    hdr("E", "Approach 2 CATS-calibrated run (genx_rescale.md 'Stochastic')")
    from load_projection.stochastic import (build_system_cells, cell_index,
                                            load_envelope_cells)
    tgt = pd.read_csv(CATS_TARGET, parse_dates=["dt_pst_hb"])
    tgt["cell"] = cell_index(tgt.month, tgt.hour_pst)
    env = load_envelope_cells()
    cells, f_star = build_system_cells(env, tgt)
    obs = tgt.groupby("cell").size()
    print(f"F* on CATS target: {f_star:.4f}   (EIA-930 history gives 0.7361)")
    print(f"target: {len(tgt)} hours, {tgt.cell.nunique()} cells, "
          f"{obs.min()}-{obs.max()} obs/cell, mean {tgt.demand_mw.mean():,.0f} MW")

    ann = pd.read_csv(STOCH_RUN / "substation_annual_mwh.csv")
    print(f"annual table: {ann.draw.nunique()} draws, "
          f"{ann.groupby(['utility','substation_name']).ngroups} substations")
    # draw-to-draw spread of the STATIC substation share
    piv = ann.pivot_table(index=["utility", "substation_name"], columns="draw",
                          values="annual_mwh", aggfunc="sum")
    sh = piv / piv.sum(axis=0)
    rel = (sh.std(axis=1) / sh.mean(axis=1)).replace([np.inf, -np.inf], np.nan)
    print(f"draw-to-draw substation share spread (cv): median "
          f"{rel.median():.3f}, p90 {rel.quantile(0.9):.3f}")

    cellf = pd.read_csv(STOCH_RUN / "substation_cell_mw.csv")
    n_draws = cellf.draw.nunique()
    subs = cellf[["utility", "substation_name"]].drop_duplicates()
    n_cells = cellf.groupby(["month", "hour_pst"]).ngroups
    mean_cells = cellf.groupby(["utility", "substation_name", "month",
                                "hour_pst"])["mean_mw"].mean()
    n_slots = len(subs) * n_cells
    n_have = mean_cells.index.nunique()
    n_neg = int((mean_cells < 0).sum())
    print(f"per-cell table: {len(cellf):,} rows, {n_cells} cells, "
          f"{len(subs):,} substations, {n_draws} draws")
    print(f"  fallback-filled slots (sub x cell grid): {n_slots - n_have} "
          f"of {n_slots:,} ({100*(n_slots-n_have)/n_slots:.2f}%)")
    print(f"  negative cell means clipped to 0 (mean draw): {n_neg} "
          f"({100*n_neg/n_have:.2f}%)")


def section_f_maps() -> None:
    hdr("F", "Nodal map artifacts (genx_rescale.md 'The map axis')")
    summ = CHECKS / "build_identity_catchment_maps/map_summary.csv"
    if summ.exists():
        print(pd.read_csv(summ).to_string(index=False))
    lp = CHECKS / "build_identity_catchment_maps/lp_stats.csv"
    if lp.exists():
        print(pd.read_csv(lp).to_string(index=False))
    pairs = CHECKS / "build_identity_catchment_maps/identity_pairs.csv"
    if pairs.exists():
        p = pd.read_csv(pairs)
        print(f"identity matches: {len(p):,} buses across "
              f"{p.groupby(['utility','substation_name']).ngroups:,} substations")
        print(p.groupby("utility").agg(
            n_subs=("substation_name", "nunique"), n_buses=("node", "nunique"),
            med_dist_km=("dist_km", "median")).round(3).to_string())
    print("(regenerate with scripts/load_projection/nodal/"
          "build_identity_catchment_maps.py)")


def section_g_runs() -> None:
    hdr("G", "Rescale runs on disk (genx_runbook.md run table + sanity numbers)")
    rows = []
    for d in sorted(RESCALED.iterdir()):
        mf = d / "manifest.json"
        if not mf.exists():
            continue
        m = json.loads(mf.read_text(encoding="utf-8"))
        dev = max(s.get("max_abs_hourly_dev_printed_mw", 0)
                  for s in m["seasons"].values())
        pre = max(s.get("max_abs_hourly_dev_preround_mw", 0)
                  for s in m["seasons"].values())
        prov = m.get("provenance", {})
        rows.append({
            "run": m["run_tag"],
            "printed_dev_mw": dev, "preround_dev_mw": f"{pre:.1e}",
            "topoff_frac": round(prov.get("topoff_fraction",
                                          prov.get("beta_equal_pool", 0.0)), 4),
            "n_share_buses": prov.get("n_nodes_with_share",
                                      prov.get("n_buses_swept", "")),
        })
    df = pd.DataFrame(rows)
    print(df.to_string(index=False))
    bad = df[df.printed_dev_mw > 0]
    print(f"\n{len(df)} runs; printed conservation violations: {len(bad)}")


def section_h_divergence() -> None:
    hdr("H", "Input divergence (genx_runbook.md / genx_comparison.md tables)")
    f = CHECKS / "genx_rescale/demand_comparison_summary.csv"
    if not f.exists():
        print("demand_comparison_summary.csv missing -- run compare_genx_demand.py")
        return
    s = pd.read_csv(f)
    cols = ["energy_relocated_pct", "county_energy_relocated_pct",
            "spearman_bus_energy", "n_buses_method", "jaccard_support"]
    print(s.groupby("run")[cols].mean().round(3).to_string())
    print("(means over the four seasons; regenerate with compare_genx_demand.py)")


def section_i_coverage() -> None:
    """Bus counts AND energy shares for the pools -- the numbers that explain
    why 'buses loaded' differs so much between allocations."""
    hdr("I", "Pool coverage, by bus count and by ENERGY "
             "(genx_rescale.md 'Which buses', genx_comparison.md reading guide)")
    from rescale_genx_demand import candidate_buses
    nodes = pd.read_csv(ROOT / "data/raw/CATS/CATS_buses.csv")
    for c in nodes.columns:
        if pd.api.types.is_string_dtype(nodes[c]):
            nodes[c] = nodes[c].str.strip().str.strip("'").str.strip()
    ids = nodes.bus_i.astype(str)

    tree = scenario_seasons()
    energy = {}
    for season, case in tree.canonical.items():
        d = read_demand(tree.demand_path(case))
        for z, v in zip(d.zones, d.values.sum(axis=0)):
            energy[z] = energy.get(z, 0.0) + float(v)
    E = pd.Series(energy)
    total = E.sum()
    loaded = set(E[E > 0].index)

    sub = set(ids[(nodes.Type == "Substation") & (nodes.Import != "IMPORT")])
    added_loaded = set(ids[nodes.Type == "AddedNode"]) & loaded
    pool = sub | added_loaded
    cand = set(candidate_buses().node)          # pool restricted to a county polygon

    def share(bus_set):
        return 100 * E.reindex(sorted(bus_set)).fillna(0).sum() / total

    print("Why the control loads 2,471 buses while the candidate pool is 3,778:")
    print(f"  Type=Substation non-IMPORT           {len(sub):5d} buses, "
          f"CATS loads {len(sub & loaded):5d}, leaves {len(sub - loaded):5d} EMPTY")
    print(f"  AddedNode that CATS loads            {len(added_loaded):5d} buses, "
          f"{share(added_loaded):5.1f}% of energy")
    print(f"  => control-loaded buses              {len(loaded):5d} "
          f"= {len(sub & loaded)} + {len(added_loaded)}")
    print(f"  => candidate pool                    {len(pool):5d} "
          f"({len(cand)} of them inside a county polygon)")
    print(f"  => pool buses CATS leaves EMPTY      {len(pool - loaded):5d}  "
          f"<- full redistribution can light these up; a hold cannot")

    print("\nEnergy shares of the control's total (all four weeks):")
    outside = loaded - cand
    print(f"  on buses OUTSIDE the candidate pool  {share(outside):8.4f}%  "
          f"({len(outside)} buses: {sorted(outside)})")

    # the hold pool: buses some substation maps to with positive envelope weight
    for tag, label in [("genx__env__prox__hold__monthhour", "env hold"),
                       ("genx__stoch__prox__w2-mean__monthhour", "stoch w2")]:
        p = RESCALED / tag / "node_shares.csv"
        if not p.exists():
            continue
        hp = set(pd.read_csv(p, dtype={"node": str}).node.unique())
        print(f"  {label:9s} pool: {len(hp):5d} buses, {share(hp):6.2f}% of control "
              f"energy re-allocated; {100 - share(hp):6.2f}% left at its control value")

    # the county-first share vector: which candidate buses get nothing
    p = RESCALED / "genx__reedsco__prox__aratio__monthhour/node_shares.csv"
    if p.exists():
        sv = set(pd.read_csv(p, dtype={"node": str}).node.unique())
        zero = cand - sv
        print(f"\ncounty-first (aratio) share vector: {len(sv)} of {len(cand)} "
              f"candidate buses; {len(zero)} get zero share: {sorted(zero)}")
        if zero:
            print(f"  their control energy: {share(zero):.4f}% "
                  f"(zero-envelope substations in counties with no uncovered bus)")


SECTIONS = {"A": section_a_control, "B": section_b_pool, "C": section_c_calendar,
            "D": section_d_envelope_fallback, "E": section_e_stochastic,
            "F": section_f_maps, "G": section_g_runs, "H": section_h_divergence,
            "I": section_i_coverage}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sections", default=",".join(SECTIONS),
                    help="comma list of section letters (default: all)")
    args = ap.parse_args()
    for s in args.sections.split(","):
        SECTIONS[s.strip().upper()]()


if __name__ == "__main__":
    main()
