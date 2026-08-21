"""Build the identity- and optimization-based substation->CATS-bus map artifacts.

Three alternatives to the proximity map, written once and cached as inputs (none
depends on any rescale-run axis, so they are never rebuilt per run):

  substation_node_map__nameprox.csv      IDENTITY FIRST: a substation whose CEC
      record is the very record a CATS bus was built from is assigned to that
      bus (all its voltage-level buses, equal split); everyone else keeps their
      proximity assignment.  The identity chain is
        our name --(norm()/cecSourceDictionary)--> CEC record
                 --(coincident coordinate)--> CATS bus,
      valid because CATS bus coordinates descend from the CEC/HIFLD lineage
      (all 3,171 Type='Substation' buses sit within 8 m of a CEC record).
  substation_node_map__catchment.csv     TRANSPORTATION LP: every candidate bus
      is assigned to exactly one substation (min total distance, each substation
      catches >= 1 bus); a substation's load then returns to its catchment as an
      equal split, so EVERY candidate bus stays loaded.
  substation_node_map__namecatchment.csv identity matches enter the LP as FORCED
      assignments (x_ij = 1 fixed); the LP places the rest.

Output schema matches substation_node_map.csv: utility, substation_name, node,
share, dist_km, n_tied (= catchment/identity-group size), is_synthetic,
assignment_method in {name, prox, catchment}.

CLI parameters
  --k-nearest N        substations per bus admitted as LP arcs (default 20)
  --sub-arcs N         each substation's own nearest buses always admitted, so
                       the >=1 constraint stays feasible (default 3)
  --cec-bus-dist-km    max bus->CEC-record distance to accept the bus as BEING
                       that record (default 0.1; observed p99 is 0.007)
  --name-sanity-km     reject an identity match whose bus lies farther than this
                       from the substation's own coordinate -- catches CEC name
                       collisions between distant sites (default 30)

Outputs
  data/processed/load_projection/nodal/CATS/substation_node_map__{nameprox,
      catchment,namecatchment}.csv
  data/checks/build_identity_catchment_maps/  match + LP statistics

Usage
  python scripts/load_projection/nodal/build_identity_catchment_maps.py
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import linprog
from scipy.sparse import csr_matrix

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/data/substations"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/genx"))
from build_cec_name_dictionary import norm  # noqa: E402  (single match definition)
from rescale_genx_demand import candidate_buses  # noqa: E402  (single pool definition)

NODAL_DIR = ROOT / "data/processed/load_projection/nodal/CATS"
CHECKS = ROOT / "data/checks/build_identity_catchment_maps"
PROX_MAP = NODAL_DIR / "substation_node_map.csv"
ATTR_FILE = ROOT / "data/processed/substations/substation_attributes_clean.csv"
CEC_FILE = ROOT / "data/processed/substation_misc/ca_substations_cec.csv"
CATS_CEC = ROOT / "data/checks/compare_cats_cec/cats_cec_join.csv"
CATS_BUSES = ROOT / "data/raw/CATS/CATS_buses.csv"
DICT_FILE = ROOT / "data/cecSourceDictionary.csv"


def haversine_km(lat1, lon1, lat2, lon2):
    """Vectorized great-circle distance; accepts arrays broadcastable together."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp, dl = np.radians(lat2 - lat1), np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * 6371.0 * np.arcsin(np.sqrt(a))


def load_substations() -> pd.DataFrame:
    """The 1,325 real substations the proximity map assigns, with coordinates.

    The set is taken from the prox map itself (not attributes) so every map
    variant covers exactly the same substations -- the comparison then isolates
    the assignment method.  Coordinate = utility's own, falling back to basin.
    """
    prox = pd.read_csv(PROX_MAP, dtype={"node": str})
    subs = (prox[~prox.is_synthetic][["utility", "substation_name"]]
            .drop_duplicates().reset_index(drop=True))
    attr = pd.read_csv(ATTR_FILE)
    attr["lat"] = attr.util_lat.fillna(attr.basin_lat)
    attr["lon"] = attr.util_lon.fillna(attr.basin_lon)
    subs = subs.merge(attr[["utility", "substation_name", "lat", "lon"]],
                      on=["utility", "substation_name"], how="left")
    n_missing = int(subs.lat.isna().sum())
    if n_missing:
        raise ValueError(f"{n_missing} prox-mapped substations lack a coordinate")
    return subs


def load_buses() -> pd.DataFrame:
    """Candidate pool (3,778 buses) with coordinates."""
    cand = candidate_buses()[["node", "county_name", "fips_int"]]
    cats = pd.read_csv(CATS_BUSES)
    cats["node"] = cats.bus_i.astype(str)
    return cand.merge(cats[["node", "Lat", "Lon"]], on="node", how="left").rename(
        columns={"Lat": "lat", "Lon": "lon"})


def owner_base(o) -> str:
    """'pge_assumed' -> 'pge'; NaN -> ''."""
    return str(o).lower().replace("_assumed", "") if pd.notna(o) else ""


def identity_pairs(subs: pd.DataFrame, buses: pd.DataFrame, args) -> pd.DataFrame:
    """(utility, substation_name, node, dist_km): substation and bus share one
    CEC record.

    Match definition mirrors audit_substation_coverage.py: a substation's CEC
    name is its own norm()'d name when a confirmed same-owner CEC record carries
    it, else the cecSourceDictionary CECName.  A bus carries a CEC name when its
    nearest CEC record is within --cec-bus-dist-km (i.e. the bus IS that
    record).  A name collision between distant same-name sites is rejected by
    the --name-sanity-km gate on the substation->bus distance.
    """
    cec = pd.read_csv(CEC_FILE)
    cec["name_norm"] = cec.name.map(norm)
    confirmed = {u: set(cec.loc[cec.owner_std == u, "name_norm"])
                 for u in ("pge", "sce", "sdge")}
    dic = pd.read_csv(DICT_FILE)
    dic["util_lc"] = dic.Utility.str.lower()
    dic_map = {(r.util_lc, norm(r.SourceName)): norm(r.CECName)
               for r in dic.itertuples()}

    def sub_cec_name(utility, name):
        n = norm(name)
        if (utility, n) in dic_map:
            return dic_map[(utility, n)]
        if n in confirmed.get(utility, ()):
            return n
        return None

    s = subs.copy()
    s["cec_norm"] = [sub_cec_name(u, n) for u, n in zip(s.utility, s.substation_name)]

    j = pd.read_csv(CATS_CEC)
    j["node"] = j.bus_i.astype(str)
    j = j[j.dist_km <= args.cec_bus_dist_km]
    j["cec_norm"] = j.nearest_cec_name.map(norm)
    j["owner"] = j.nearest_cec_owner.map(owner_base)
    j = j[j.node.isin(set(buses.node))][["node", "cec_norm", "owner"]]

    pairs = s.dropna(subset=["cec_norm"]).merge(
        j, left_on=["utility", "cec_norm"], right_on=["owner", "cec_norm"])
    bl = buses.set_index("node")
    pairs["dist_km"] = haversine_km(pairs.lat.values, pairs.lon.values,
                                    bl.lat.reindex(pairs.node).values,
                                    bl.lon.reindex(pairs.node).values)
    n_collision = int((pairs.dist_km > args.name_sanity_km).sum())
    pairs = pairs[pairs.dist_km <= args.name_sanity_km]
    # a bus is one physical station: if two substations claim it, the nearer wins
    pairs = pairs.sort_values("dist_km").drop_duplicates("node", keep="first")
    pairs.attrs["n_collision_rejected"] = n_collision
    return pairs[["utility", "substation_name", "node", "dist_km"]].reset_index(drop=True)


def build_nameprox(subs, prox: pd.DataFrame, pairs: pd.DataFrame) -> pd.DataFrame:
    """Identity rows for matched substations; verbatim prox rows for the rest."""
    matched = set(map(tuple, pairs[["utility", "substation_name"]].values))
    rows = pairs.copy()
    k = rows.groupby(["utility", "substation_name"])["node"].transform("size")
    rows["share"] = 1.0 / k
    rows["n_tied"] = k
    rows["is_synthetic"] = False
    rows["assignment_method"] = "name"
    keep = prox[~prox.is_synthetic]
    keep = keep[[(u, n) not in matched
                 for u, n in zip(keep.utility, keep.substation_name)]]
    cols = ["utility", "substation_name", "node", "share", "dist_km",
            "n_tied", "is_synthetic", "assignment_method"]
    return pd.concat([rows[cols], keep[cols]], ignore_index=True)


def solve_catchment(subs: pd.DataFrame, buses: pd.DataFrame, args,
                    forced: pd.DataFrame | None = None
                    ) -> tuple[pd.DataFrame, dict]:
    """Assign every candidate bus to exactly one substation, min total distance.

        min  sum_ij x_ij d_ij
        s.t. sum_j x_ij  = 1   for every free bus i      (each bus assigned once)
             sum_i x_ij >= 1   for every substation j    (each catches >= 1 bus)
             0 <= x_ij <= 1

    The constraint matrix is the incidence structure of a bipartite
    transportation problem, hence totally unimodular: the LP relaxation's
    vertex optimum is integral, so no MIP solver is needed (verified after the
    solve).  Arcs are sparsified to each bus's --k-nearest substations plus
    each substation's --sub-arcs nearest buses (feasibility floor).  `forced`
    rows (identity matches) are fixed x_ij = 1: their buses leave the LP and
    their substations' >=1 requirement is already met.
    """
    D = haversine_km(buses.lat.values[:, None], buses.lon.values[:, None],
                     subs.lat.values[None, :], subs.lon.values[None, :])
    n_b, n_s = D.shape

    forced = forced if forced is not None else pd.DataFrame(
        columns=["utility", "substation_name", "node"])
    bus_pos = {n: i for i, n in enumerate(buses.node)}
    sub_pos = {t: j for j, t in
               enumerate(zip(subs.utility, subs.substation_name))}
    forced_b = [bus_pos[n] for n in forced.node]
    forced_j = [sub_pos[(u, s)] for u, s in
                zip(forced.utility, forced.substation_name)]
    free_b = np.setdiff1d(np.arange(n_b), forced_b)
    need = np.ones(n_s, dtype=int)
    for j in forced_j:
        need[j] = 0                      # >=1 already satisfied by a forced bus

    t0 = time.time()
    k = min(args.k_nearest, n_s)
    m = min(args.sub_arcs, len(free_b))
    res = arcs = ai = aj = cost = None
    for attempt in range(4):
        # arc set: per free bus, k nearest subs; per NEEDY sub, its m nearest
        # free buses (feasibility floor). When identity matches remove many
        # buses from the free pool, nearby needy substations can collide on the
        # same few feasibility arcs (Hall's condition fails locally); widening m
        # and retrying restores feasibility without densifying the whole LP.
        arcs = set()
        order = np.argpartition(D[free_b], k - 1, axis=1)[:, :k]
        for row, i in enumerate(free_b):
            for j in order[row]:
                arcs.add((int(i), int(j)))
        mm = min(m, len(free_b))
        sub_order = np.argpartition(D[free_b], mm - 1, axis=0)[:mm]
        for j in np.flatnonzero(need):
            for row in sub_order[:, j]:
                arcs.add((int(free_b[row]), int(j)))
        arcs = sorted(arcs)
        ai = np.array([a[0] for a in arcs])
        aj = np.array([a[1] for a in arcs])
        cost = D[ai, aj]

        # equality: each free bus picks exactly one arc
        bus_row = {i: r for r, i in enumerate(free_b)}
        A_eq = csr_matrix((np.ones(len(arcs)), ([bus_row[i] for i in ai],
                                                range(len(arcs)))),
                          shape=(len(free_b), len(arcs)))
        b_eq = np.ones(len(free_b))
        # >=1 per substation still needing one: -sum x <= -1
        need_j = np.flatnonzero(need)
        sub_row = {j: r for r, j in enumerate(need_j)}
        mask = np.isin(aj, need_j)
        A_ub = csr_matrix((-np.ones(mask.sum()),
                           ([sub_row[j] for j in aj[mask]], np.flatnonzero(mask))),
                          shape=(len(need_j), len(arcs)))
        b_ub = -np.ones(len(need_j))

        res = linprog(cost, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
                      bounds=(0, 1), method="highs")
        if res.success:
            break
        m *= 4
        print(f"  LP infeasible with sub-arcs={mm}; widening to {m} and retrying")
    if not res.success:
        raise RuntimeError(
            f"catchment LP infeasible/failed: {res.message}\n"
            f"Try increasing --sub-arcs (a substation may have no reachable arc).")
    x = res.x
    n_frac = int(((x > 1e-6) & (x < 1 - 1e-6)).sum())
    chosen = x > 0.5

    out = pd.DataFrame({
        "node": buses.node.values[ai[chosen]],
        "utility": subs.utility.values[aj[chosen]],
        "substation_name": subs.substation_name.values[aj[chosen]],
        "dist_km": cost[chosen],
        "assignment_method": "catchment",
    })
    if len(forced):
        f = forced.copy()
        f["assignment_method"] = "name"
        out = pd.concat([out, f[["node", "utility", "substation_name",
                                 "dist_km", "assignment_method"]]],
                        ignore_index=True)
    k_j = out.groupby(["utility", "substation_name"])["node"].transform("size")
    out["share"] = 1.0 / k_j
    out["n_tied"] = k_j
    out["is_synthetic"] = False
    out = out[["utility", "substation_name", "node", "share", "dist_km",
               "n_tied", "is_synthetic", "assignment_method"]]
    stats = {
        "n_arcs": len(arcs), "n_free_buses": len(free_b),
        "n_forced": len(forced), "n_fractional": n_frac,
        "objective_km": float(res.fun), "solve_seconds": round(time.time() - t0, 1),
    }
    if n_frac:
        raise RuntimeError(f"{n_frac} fractional x_ij -- TU structure violated?")
    return out, stats


def summarize(tag: str, m: pd.DataFrame) -> dict:
    per_sub = m.groupby(["utility", "substation_name"])["node"].size()
    return {
        "map": tag,
        "n_substations": int(per_sub.index.nunique()),
        "n_buses": int(m.node.nunique()),
        "n_rows": len(m),
        "by_method": m.assignment_method.value_counts().to_dict(),
        "dist_median_km": round(float(m.dist_km.median()), 3),
        "dist_p95_km": round(float(m.dist_km.quantile(0.95)), 2),
        "dist_max_km": round(float(m.dist_km.max()), 1),
        "pct_over_1km": round(float((m.dist_km > 1).mean() * 100), 1),
        "pct_over_10km": round(float((m.dist_km > 10).mean() * 100), 1),
        "catchment_max": int(per_sub.max()),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--k-nearest", type=int, default=20)
    ap.add_argument("--sub-arcs", type=int, default=3)
    ap.add_argument("--cec-bus-dist-km", type=float, default=0.1)
    ap.add_argument("--name-sanity-km", type=float, default=30.0)
    args = ap.parse_args()
    CHECKS.mkdir(parents=True, exist_ok=True)

    subs = load_substations()
    buses = load_buses()
    prox = pd.read_csv(PROX_MAP, dtype={"node": str})
    print(f"{len(subs):,} substations, {len(buses):,} candidate buses")

    pairs = identity_pairs(subs, buses, args)
    n_sub_matched = pairs.groupby(["utility", "substation_name"]).ngroups
    print(f"\nidentity matches: {len(pairs):,} bus assignments across "
          f"{n_sub_matched:,} substations "
          f"({pairs.attrs.get('n_collision_rejected', 0)} rejected by the "
          f"{args.name_sanity_km:g} km sanity gate)")
    print(pairs.groupby(pairs.utility).agg(
        n_subs=("substation_name", "nunique"), n_buses=("node", "nunique"),
        med_dist_km=("dist_km", "median")).round(3).to_string())
    pairs.to_csv(CHECKS / "identity_pairs.csv", index=False)

    maps = {"nameprox": build_nameprox(subs, prox, pairs)}
    catch, st1 = solve_catchment(subs, buses, args)
    print(f"\ncatchment LP: {st1}")
    maps["catchment"] = catch
    namecatch, st2 = solve_catchment(subs, buses, args, forced=pairs)
    print(f"name+catchment LP: {st2}")
    maps["namecatchment"] = namecatch

    rows = []
    for tag, m in maps.items():
        out = NODAL_DIR / f"substation_node_map__{tag}.csv"
        m.to_csv(out, index=False)
        s = summarize(tag, m)
        rows.append(s)
        print(f"\nwrote {out.relative_to(ROOT)}")
        print(f"  {s}")
    rows.append(summarize("prox", prox[~prox.is_synthetic]))
    pd.DataFrame(rows).to_csv(CHECKS / "map_summary.csv", index=False)
    pd.DataFrame([{"lp": "catchment", **st1}, {"lp": "namecatchment", **st2}]
                 ).to_csv(CHECKS / "lp_stats.csv", index=False)
    print(f"\nwrote {CHECKS.relative_to(ROOT)}\\map_summary.csv (incl. prox baseline)")


if __name__ == "__main__":
    main()
