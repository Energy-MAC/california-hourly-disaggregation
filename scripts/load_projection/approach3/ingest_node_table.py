"""Convert a wide bus/sub-node table into Approach 3's two input files.

Input is one row per (bus-list bus, sub-node), as in `data/example.csv`::

    bus_id,bus_label,name,lat,long,source,utility,base_kv,
    bus_number_x,summer_load,'ID',bus_number_y,winter_load,Count
    32766,LIVE OAK,LIVE OAK,37.916422,-122.3026386,utility_attributes,pge,115,
    32766,22.14,'1 ',32766,18.92,6

Output is the Approach 3 contract:

  nodes.csv    node_id, base_id, subname, winter_load, summer_load, + provenance
  mapping.csv  base_id, substation_name, utility

`base_id` is the bus, `subname` is the `'ID'` column, and `node_id` is
`{bus}|{ID}` -- so the sub-node axis becomes the base/sibling axis and all of
Approach 3's sibling machinery applies unchanged.

THE NODE UNIVERSE AND THE MAPPING ARE TWO DIFFERENT THINGS
----------------------------------------------------------
This is the easiest way to get a badly wrong answer, so it is worth being
blunt about.

  * The **node universe** (`--input`) is every node you want load on. It
    defines the allocation denominator: a node absent from it receives nothing.
  * The **mapping** is the subset of buses that can borrow a measured hourly
    shape from a California utility substation. It is normally much smaller.

A mapping that covers 1 of 2,000 buses is completely fine -- that bus gets its
substation's measured pattern and the other 1,999 carry the statewide pattern,
each scaled by its own level. 100% of load is allocated either way; what the
mapping changes is only the SHAPE. But if the mapping file is passed as
`--input`, the other 1,999 buses are simply not in the universe and all of
California's load lands on the one bus.

Two supported ways to express it:

  1. ONE file, the universe, with `name` and `lat`/`long` left BLANK on rows the
     mapping does not cover. A blank key means "deliberately unmapped" and is
     reported as such, never as a failed match.
  2. TWO files: `--input` the universe (levels only is fine -- it needs just
     `bus_id`, `'ID'`, `summer_load`, `winter_load`) and
     `--mapping-input` the smaller match file, joined on by bus number. A bus
     in the mapping that is absent from the universe is REFUSED, because that
     is the signature of the mistake above.

Both routes give identical levels and edges (verified). `--expect-buses N` is
the cheapest guard: it refuses unless the universe has exactly N buses.

WHICH COLUMNS ARE ACTUALLY NEEDED
---------------------------------
Measured on `data/example.csv` (see `--audit`):

  REQUIRED   bus_id, 'ID', summer_load, winter_load, and ONE join key
  JOIN KEY   lat+long  OR  name (+utility).  They are NOT equivalent -- see below
  OPTIONAL   utility      only needed when joining by NAME and the name is
                           shared across utilities (48 such names exist)
  IGNORED    base_kv   Approach 3 has no voltage axis; the voltage-aware
                           mode belongs to the nodal map, which is unreachable
                           here anyway
  REDUNDANT  bus_number_x, bus_number_y (== bus_id), bus_label
             (== name), Count (== rows per bus), source (a constant)

HOW A BUS IS MATCHED, IN ORDER
------------------------------
0. **`load_station`** -- the input's own key column, when present. It names the
   `substation_name` to join the profiles table on, together with `utility`.
   This supersedes everything else, and a BLANK value is a definite "no load
   profile for this station", not an unattempted lookup.
1. **`station_name`** (+ `utility`) -- exact/normalized fleet match, then the
   inverted `basinSourceDictionary`. The fallback for blank `load_station`.
2. **coordinates** -- available via `--match coords`, but NOT recommended.

A SUBSTATION NAME IS NOT A KEY IN CALIFORNIA
--------------------------------------------
`Mission` is three distinct stations, `Potrero` three, `Newhall` and `Antelope`
two each. By bare name the nearest same-name candidates sit 6-1,035 km apart
(`ANTELOPE` 188 km, `BELMONT` 501 km, `LINCOLN` 143 km). That is why rule 0
exists and why the name route is only the fallback.

DO NOT REACH FOR `--match coords` AS THE FIX
--------------------------------------------
It takes the nearest profiled substation within `--max-coord-dist-km` (5 km by
default), and in dense areas the nearest station is routinely the wrong one:
Moss Landing -> `DOLAN ROAD` at 898 m, Larkin -> `SF X (MISSION)` at 721 m,
Alamitos -> `Stadium` at 794 m, Antelope -> `Lunar` at 332 m. All four are
different stations. Proximity alone, with no name or identity agreement, is not
evidence at this scale. Coordinates are carried as provenance and used only to
report a disagreement.

Run
---
  # what each column is worth, and where the keys disagree
  python scripts/load_projection/approach3/ingest_node_table.py \\
      --input data/example.csv --audit

  # emit the two Approach 3 files
  python scripts/load_projection/approach3/ingest_node_table.py \\
      --input data/example.csv --out data/checks/approach3/emilia2
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

sys.path.insert(0, str(ROOT / "scripts/data/substations"))
from build_cec_name_dictionary import norm  # noqa: E402  (single match definition)

PROFILES = ROOT / "data/processed/substations/substation_load_profiles_clean.csv"
ATTRS = ROOT / "data/processed/substations/substation_attributes_clean.csv"
BASIN_DICT = ROOT / "data/basinSourceDictionary.csv"

#: Canonical input column -> role. Anything else is carried as provenance.
#: Nothing here is specific to any one source system: this reads a flat list of
#: buses, each split into sub-nodes, with two seasonal levels apiece.
BUS_COL = "bus_id"
ID_COL = "sub_id"
STATION_COL = "load_station"
NAME_COL = "station_name"
LAT_COL, LON_COL = "lat", "lon"
UTIL_COL = "utility"
KV_COL = "base_kv"
LEVEL_COLS = ("summer_load", "winter_load")

#: Accepted spellings for each canonical column, so an existing export does not
#: have to be renamed to be read. First match wins, canonical name checked first.
COLUMN_ALIASES = {
    BUS_COL: ("bus_id", "bus_number", "busnum", "bus"),
    ID_COL: ("sub_id", "'ID'", "ID", "id", "sub_node", "subname"),
    STATION_COL: ("load_station", "load_station_name"),
    NAME_COL: ("station_name", "name", "substation_name"),
    LAT_COL: ("lat", "latitude"),
    LON_COL: ("lon", "long", "longitude"),
    UTIL_COL: ("utility", "owner"),
    KV_COL: ("base_kv", "baskv", "kv", "voltage_kv"),
    "summer_load": ("summer_load",),
    "winter_load": ("winter_load",),
    "source": ("source",),
    "bus_label": ("bus_label", "bus_name"),
}


def canonicalize(df: pd.DataFrame, path) -> pd.DataFrame:
    """Rename recognized input spellings to the canonical names.

    An export may call the bus `bus_id` or `bus_number`; the sub-node
    id arrives as `'ID'`, quotes and all. None of that is meaningful to the
    method, so it is normalized here once and never spoken of again.
    """
    lower = {str(c).strip().lower(): c for c in df.columns}
    ren = {}
    for canon, alts in COLUMN_ALIASES.items():
        for a in alts:
            hit = lower.get(a.strip().lower())
            if hit is not None:
                if hit != canon:
                    ren[hit] = canon
                break
    clash = [c for c in ren.values() if c in df.columns and c not in ren]
    if clash:
        raise SystemExit(
            f"{path}: cannot rename to {clash} -- a column with that canonical "
            f"name already exists alongside an alias. Drop one.")
    return df.rename(columns=ren)

REDUNDANT = ("bus_number_x", "bus_number_y", "Count")
#: the bus's own label, carried through as provenance. It is NOT the same thing
#: as `station_name`: a label is always present (e.g. "GATE 42A") while
#: `station_name` is blank unless the bus was matched to a utility substation.
#: The match keys on `load_station` first and `station_name` second -- never on
#: the label is never a join key, because a bus label need not be a substation
#: name at all.
LABEL_COL = "bus_label"
MATCH_MODES = ("coords", "name", "coords-then-name")

#: the only utilities we hold month-hour load envelopes for. A bus whose
#: utility is anything else (iid, ladwp, smud, ...) can never be matched.
IOU = {"pge", "sce", "sdge"}


def hav_km(lat1, lon1, lat2, lon2) -> np.ndarray:
    R = 6371.0
    p1 = np.radians(np.asarray(lat1, dtype=float))
    p2 = np.radians(np.asarray(lat2, dtype=float))
    dp = p2 - p1
    dl = np.radians(np.asarray(lon2, dtype=float) - np.asarray(lon1, dtype=float))
    return 2 * R * np.arcsin(np.sqrt(
        np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2))


def clean_id(v) -> str:
    """`\"'1 '\"` -> `1`. The ID column arrives quoted and space-padded."""
    return str(v).strip().strip("'").strip()


def usable_envelopes(min_net_gross: float = 0.20) -> pd.DataFrame:
    """The (utility, substation) pairs that can actually lend an hourly shape.

    Being in the profiles file is NOT enough. A match is only USEFUL if the
    substation has a non-degenerate envelope, because Approach 3's shape guard
    (G8) would otherwise replace it with a flat 1.0 -- i.e. the bus would be
    counted as "mapped" while carrying the statewide shape anyway, which makes
    the coverage numbers a lie.

    Measured on the current fleet: 1,308 of 1,347 substations are usable. The
    39 that are not are 6 with no data at all (all cells NaN, e.g. sce/Autobody),
    plus substations whose envelope nets to zero or below (e.g. sce/Alola at
    exactly 0.000, pge/HENRIETTA at -5,975 MW of net reverse flow) or whose
    net-to-gross falls under `min_net_gross` (pge/PIT NO 5 at 0.024).
    """
    from load_projection.weights import load_profiles
    pr = load_profiles(PROFILES, "avg_load")
    pr["utility"] = pr.utility.str.lower()
    g = pr.groupby(["utility", "substation_name"]).avg_load
    st = pd.DataFrame({
        "n_finite": g.apply(lambda v: int(np.isfinite(v).sum())),
        "net": g.sum(),
        "gross": g.apply(lambda v: float(np.abs(v).sum()))})
    st["ntg"] = np.where(st.gross > 0, st.net.abs() / st.gross, 0.0)
    ok = st[(st.n_finite > 0) & (st.net > 0) & (st.ntg >= min_net_gross)]
    return ok.reset_index()[["utility", "substation_name"]]


def unusable_envelopes(min_net_gross: float = 0.20) -> pd.DataFrame:
    """Profiled substations whose envelope is NOT usable -- for diagnosis only.

    A name that lands here is in the fleet but cannot lend a shape, which must
    read differently from a name that is not in the fleet at all.
    """
    allp = pd.read_csv(PROFILES, usecols=["utility", "substation_name"]).drop_duplicates()
    allp["utility"] = allp.utility.str.lower()
    ok = usable_envelopes(min_net_gross)
    return allp.merge(ok, on=["utility", "substation_name"], how="left",
                      indicator=True).query("_merge == 'left_only'")[
        ["utility", "substation_name"]]


def load_fleet(min_net_gross: float = 0.20) -> tuple[pd.DataFrame, pd.DataFrame]:
    prof = usable_envelopes(min_net_gross)
    attrs = pd.read_csv(ATTRS)
    attrs["utility"] = attrs.utility.str.lower()
    attrs = attrs[np.isfinite(attrs.util_lat) & np.isfinite(attrs.util_lon)]
    # only substations that actually carry a profile can lend a shape
    coords = attrs.merge(prof, on=["utility", "substation_name"], how="inner")
    return prof, coords[["utility", "substation_name", "util_lat", "util_lon"]]


def read_input(path: Path) -> pd.DataFrame:
    df = canonicalize(pd.read_csv(path), path)
    for c in (BUS_COL, ID_COL, *LEVEL_COLS):
        if c not in df.columns:
            raise SystemExit(
                f"{path}: missing required column {c!r}. Required: "
                f"{[BUS_COL, ID_COL, *LEVEL_COLS]} plus a join key "
                f"({STATION_COL}, or {NAME_COL}, or {LAT_COL}+{LON_COL}). "
                f"Got {list(df.columns)}. "
                f"Accepted spellings: "
                + "; ".join(f"{k} <- {list(v)}"
                            for k, v in COLUMN_ALIASES.items()))
    df["base_id"] = df[BUS_COL].astype(str).str.strip()
    df["subname"] = df[ID_COL].map(clean_id)
    df["node_id"] = df.base_id + "|" + df.subname
    for c in LEVEL_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # A BLANK join key means "this node is deliberately unmapped", which is the
    # normal case: the mapping covers a subset of the node universe. Normalize
    # it now, because a blank read back from CSV is NaN and `str(NaN)` is the
    # string "nan" -- which would otherwise be matched as if it were a
    # substation NAME.
    for c in (NAME_COL, UTIL_COL):
        if c in df.columns:
            v = df[c].astype("string").fillna("").str.strip()
            df[c] = v.mask(v.str.lower().isin(["nan", "none", "null", "na",
                                               "n/a", "-"]), "")
    for c in (LAT_COL, LON_COL):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    dup = df.node_id[df.node_id.duplicated()]
    if len(dup):
        raise SystemExit(
            f"{path}: {len(dup)} duplicate (bus, ID) pair(s), e.g. "
            f"{dup.unique()[:5].tolist()}. Each sub-node must appear once; a "
            f"duplicate would double-count its load.")
    return df


def match_by_coords(df, coords, max_km):
    """Nearest profiled substation to each bus's own coordinates."""
    if LAT_COL not in df.columns or LON_COL not in df.columns:
        return None
    buses = df.drop_duplicates("base_id")[["base_id", LAT_COL, LON_COL]].copy()
    out = []
    cl, co = coords.util_lat.to_numpy(), coords.util_lon.to_numpy()
    for r in buses.itertuples():
        la, lo = getattr(r, LAT_COL), getattr(r, LON_COL)
        if not (np.isfinite(la) and np.isfinite(lo)):
            # no coordinates supplied: deliberately unmapped on this route
            out.append((r.base_id, None, None, np.nan))
            continue
        d = hav_km(la, lo, cl, co)
        j = int(np.argmin(d))
        out.append((r.base_id, coords.utility.iloc[j],
                    coords.substation_name.iloc[j], float(d[j])))
    m = pd.DataFrame(out, columns=["base_id", "utility", "substation_name",
                                   "coord_dist_km"])
    m["coord_ok"] = m.coord_dist_km <= max_km
    return m


def match_by_station(df, prof):
    """Rule 0: the input's own `load_station` column, when it has one.

    `load_station` is a KEY -- the `substation_name` to join
    `substation_load_profiles_clean.csv` on together with `utility` -- produced
    upstream by coordinate identity, or by a name lookup confined to the one
    table where the name IS the key. A substation name on its own is NOT a key
    in California: `Mission` is three distinct stations, `Potrero` three,
    `Newhall` and `Antelope` two each, and by bare name the nearest same-name
    candidates sit 6-1,035 km apart. So where `load_station` is present it
    supersedes both the name and the coordinate routes.

    A BLANK `load_station` is a definite "no load profile for this station",
    not an unattempted lookup: the profiles table holds exactly the stations of
    the attributes table, so membership and having a profile are the same thing.
    Those buses fall through to the name route, which recovers the handful the
    upstream join leaves out.
    """
    if STATION_COL not in df.columns:
        return None
    cols = ["base_id", STATION_COL] + ([UTIL_COL] if UTIL_COL in df.columns else [])
    buses = df.drop_duplicates("base_id")[cols]
    pairs = set(map(tuple, prof[["utility", "substation_name"]].values))
    by_name: dict = {}
    for u, sn in pairs:
        by_name.setdefault(sn, []).append(u)
    out = []
    for r in buses.itertuples():
        st = str(getattr(r, STATION_COL) or "").strip()
        util = (str(getattr(r, UTIL_COL, "") or "").strip().lower()
                if UTIL_COL in buses.columns else "")
        if not st or st.lower() in ("nan", "none", "null", "na", "n/a", "-"):
            out.append((r.base_id, None, None, "no load_station supplied"))
            continue
        owners = by_name.get(st, [])
        if util and util in owners:
            out.append((r.base_id, util, st, "ok"))
        elif len(owners) == 1:
            out.append((r.base_id, owners[0], st, "ok"))
        elif not owners:
            out.append((r.base_id, None, None,
                        f"load_station {st!r} is not a station with a usable "
                        f"load envelope"))
        else:
            out.append((r.base_id, None, None,
                        f"load_station {st!r} exists under {sorted(owners)} "
                        f"and the row's utility {util!r} is not one of them"))
    return pd.DataFrame(out, columns=["base_id", "utility", "substation_name",
                                      "station_status"])


def basin_route(prof, norm):
    """`norm(BasinName) -> (utility, profiled substation_name)`, rule 2.

    A bus list that sources some rows from the basin dataset carries the BASIN
    spelling in `name`, and 86 of the 90 dictionary rows have
    `BasinName != SourceName` ("Artesian" vs "Artesian Ranch", "Capistrano" vs
    "San Juan Capistrano", "Balch 1" vs "BALCH NO 1"). Without this route those
    rows fail to match even though the substation is right there in the fleet.

    Reuses the production inverter, so the blank-BasinName VETO and the
    many-to-one refusal behave identically to the external-loads path. The index
    is built from the USABLE-envelope fleet only, so the dictionary route cannot
    smuggle in a substation that has no shape to lend.
    """
    import load_projection.external_loads as ext
    name_index, _, _ = ext.build_name_index(prof, norm)
    try:
        lookup, ambiguous = ext.invert_basin_dictionary(BASIN_DICT, name_index,
                                                        norm)
    except FileNotFoundError:
        return {}, {}
    return lookup, ambiguous


def match_by_name(df, prof, norm, unusable=None, basin=None,
                  sub_coords=None):
    """Exact-then-normalized name match, refusing a name several utilities use.

    Ambiguity is decided on `norm(name)`, NOT on the verbatim string. Our fleet
    carries pge "LIVE OAK" and sce "Live Oak", which differ only in CASE: a
    verbatim index treats them as two distinct names and would silently resolve
    "LIVE OAK" to pge on that coincidence alone. `norm` is the project's single
    name-match definition (it also strips P.T., the word "substation" and
    punctuation), and under it those two collide and are refused unless the row
    supplies `utility` -- which is the standing "names are refused, not guessed"
    rule. 48 names are shared across utilities fleet-wide.
    """
    if NAME_COL not in df.columns:
        return None
    # lat/long are carried ONLY so a failed utility match can report how far the
    # row's own coordinates are from the other utilities' candidates. They never
    # take part in the match itself.
    buses = df.drop_duplicates("base_id")[
        ["base_id", NAME_COL]
        + ([UTIL_COL] if UTIL_COL in df.columns else [])
        + [c for c in (LAT_COL, LON_COL) if c in df.columns]]
    idx: dict = {}
    for u, sn in prof[["utility", "substation_name"]].itertuples(index=False):
        idx.setdefault(norm(sn), []).append((u, sn))
    blookup, bambig = basin if basin is not None else ({}, {})
    sc = sub_coords or {}
    unu: dict = {}
    if unusable is not None:
        for u, sn in unusable[["utility", "substation_name"]].itertuples(index=False):
            unu.setdefault(norm(sn), []).append((u, sn))
    out = []
    for r in buses.itertuples():
        raw = str(getattr(r, NAME_COL)).strip()
        util = (str(getattr(r, UTIL_COL)).strip().lower()
                if UTIL_COL in buses.columns else "")
        if not raw:
            # the normal case for a node the mapping does not cover; it is NOT
            # a failure, and it must read differently from one that failed
            out.append((r.base_id, None, None, "no name supplied"))
            continue
        if util and util not in IOU:
            out.append((r.base_id, None, None,
                        f"utility {util!r} is outside the three IOUs"))
            continue
        cands = idx.get(norm(raw), [])
        if not cands and blookup.get(norm(raw)):
            hit = blookup[norm(raw)]
            if (not util) or util == hit[0]:
                out.append((r.base_id, hit[0], hit[1],
                            "ok; matched via basinSourceDictionary"))
                continue
        if not cands and norm(raw) in bambig:
            out.append((r.base_id, None, None,
                        f"basinSourceDictionary maps {raw!r} back to "
                        f"{bambig[norm(raw)]} -- more than one profiled "
                        f"substation, so it is refused rather than guessed"))
            continue
        if not cands:
            why = "name not in the profiled fleet"
            if unu.get(norm(raw)):
                owners = sorted({u for u, _ in unu[norm(raw)]})
                why = (f"matched {owners} but that substation has NO USABLE "
                       f"LOAD ENVELOPE (no data, or it nets to zero/below), so "
                       f"it cannot lend an hourly shape -- treated as unmapped")
            out.append((r.base_id, None, None, why))
            continue
        utils = {u for u, _ in cands}
        if util:
            hit = [(u, sn) for u, sn in cands if u == util]
            if hit:
                out.append((r.base_id, hit[0][0], hit[0][1], "ok"))
            else:
                # THAT utility may well have this substation -- just with an
                # unusable envelope. Saying "has no substation matching" would
                # be plainly false, and sends the reader looking for a name
                # error that is not there.
                own = [(u, sn) for u, sn in unu.get(norm(raw), []) if u == util]
                if own:
                    out.append((r.base_id, None, None,
                                f"{util}/{own[0][1]} EXISTS but has no usable "
                                f"load envelope, so it cannot lend a shape "
                                f"(other utilities with this name: "
                                f"{sorted(utils)})"))
                else:
                    # If the row's own coordinates sit on one of the other
                    # utilities' candidates, the `utility` label is simply
                    # wrong and the match is recoverable. Distance makes that
                    # decidable instead of leaving it to judgement.
                    near = ""
                    la = getattr(r, LAT_COL, np.nan) if LAT_COL in buses.columns else np.nan
                    lo = getattr(r, LON_COL, np.nan) if LON_COL in buses.columns else np.nan
                    if sc and np.isfinite(la) and np.isfinite(lo):
                        ds = [(float(hav_km(la, lo, *sc[c])), c)
                              for c in cands if c in sc]
                        if ds:
                            ds.sort()
                            d0, c0 = ds[0]
                            near = (f"; the row's own coordinates are "
                                    f"{d0:.1f} km from {c0[0]}/{c0[1]}"
                                    + (" -- so the utility label looks wrong "
                                       "and this match is recoverable"
                                       if d0 <= 1.0 else
                                       " -- far enough that this is probably a "
                                       "different site, correctly unmapped"))
                    out.append((r.base_id, None, None,
                                f"utility {util!r} has no substation matching "
                                f"{raw!r} (known: {sorted(utils)}){near}"))
        elif len(utils) > 1:
            out.append((r.base_id, None, None,
                        f"name matches {sorted(utils)}; supply "
                        f"'{UTIL_COL}' -- with no coordinates there is no "
                        f"tie-break, and this refuses rather than guess"))
        elif len(cands) > 1:
            out.append((r.base_id, None, None,
                        f"name matches {len(cands)} substations of "
                        f"{sorted(utils)[0]} ({[sn for _, sn in cands]}); "
                        f"ambiguous within one utility"))
        else:
            out.append((r.base_id, cands[0][0], cands[0][1], "ok"))
    return pd.DataFrame(out, columns=["base_id", "utility", "substation_name",
                                      "name_status"])


def report_unmapped(m, route: str) -> list:
    """Why each unmapped bus is unmapped, most common first.

    "no key supplied" is the expected case and must read differently from a
    real failure: the first means the mapping does not cover that bus, the
    second means it tried and could not.
    """
    if m is None:
        return []
    if route == "name":
        bad = m[~m.name_status.str.startswith("ok")]
        return list(bad.name_status.value_counts().items())
    bad = m[~m.coord_ok.fillna(False)]
    out = []
    n_none = int(bad.coord_dist_km.isna().sum())
    if n_none:
        out.append(("no coordinates supplied (not covered by the mapping)",
                    n_none))
    far = bad[bad.coord_dist_km.notna()]
    if len(far):
        out.append((f"nearest profiled substation too far "
                    f"(median {far.coord_dist_km.median():.1f} km)", len(far)))
    return out


def audit(df, prof, coords, args, norm, unusable=None, basin=None,
          sub_coords=None) -> None:
    print("=" * 74)
    print("COLUMN AUDIT -- what this file needs and what it does not")
    print("=" * 74)
    cols = list(df.columns)
    n_bus = df.base_id.nunique()
    print(f"rows {len(df):,}   buses {n_bus:,}   "
          f"sub-nodes per bus: median "
          f"{int(df.groupby('base_id').size().median())}, "
          f"max {int(df.groupby('base_id').size().max())}")
    print()
    print("REQUIRED")
    for c in (BUS_COL, ID_COL, *LEVEL_COLS):
        print(f"  {c:<22} present={c in cols}")
    print("JOIN KEY (need at least one)")
    for c in (LAT_COL, LON_COL, NAME_COL):
        print(f"  {c:<22} present={c in cols}")
    print("OPTIONAL / IGNORED")
    print(f"  {UTIL_COL:<22} present={UTIL_COL in cols}   "
          f"(needed only for a NAME join on a shared name)")
    print(f"  {KV_COL:<22} present={KV_COL in cols}   "
          f"(IGNORED -- Approach 3 has no voltage axis)")
    print("REDUNDANT (derivable or constant; safe to drop)")
    for c in REDUNDANT:
        if c in cols:
            if c in ("bus_number_x", "bus_number_y"):
                same = (df[c].astype(str).str.strip() == df.base_id).all()
                print(f"  {c:<22} == {BUS_COL}: {same}")
            elif c == "bus_label":
                same = (df[c].astype(str) == df[NAME_COL].astype(str)).all() \
                    if NAME_COL in cols else "n/a"
                print(f"  {c:<22} == {NAME_COL}: {same}")
            elif c == "Count":
                same = (df.groupby("base_id").base_id.transform("size")
                        == df[c]).all()
                print(f"  {c:<22} == rows per bus: {same}")
            else:
                print(f"  {c:<22} values: {df[c].unique()[:3].tolist()}")
    if "source" in cols:
        print()
        print("SOURCE INVENTORY (drives --zero-sources)")
        src = df.source.astype("string").fillna("").replace("", "(blank)")
        for v, k in src.value_counts().items():
            nb = df[src == v].base_id.nunique()
            print(f"  {str(v):<28} {k:>6,} sub-node(s) in {nb:>5,} bus(es)")
        print("  (this column is read verbatim from your input; it is not set "
              "here)")
    print()
    print("LEVELS")
    for c in LEVEL_COLS:
        neg = df[c] < 0
        print(f"  {c:<22} net {df[c].sum():10.2f}   positive-only "
              f"{df[c].clip(lower=0).sum():10.2f}   negative sub-nodes "
              f"{int(neg.sum())}")
    if (df[list(LEVEL_COLS)] < 0).any().any():
        bad = df[(df[list(LEVEL_COLS)] < 0).any(axis=1)]
        print(f"  negative sub-nodes: {bad.node_id.tolist()[:8]}")
        print(f"  => under --negative-nodes net-base (the default) a base is")
        print(f"     allocated its signed NET and that net is split among its")
        print(f"     POSITIVE siblings; the negative one is written 0.0 with")
        print(f"     its level preserved in node_index.csv.")

    print()
    print("=" * 74)
    print("JOIN KEY COMPARISON -- are name and coordinates the same answer?")
    print("=" * 74)
    cm = match_by_coords(df, coords, args.max_coord_dist_km)
    nm = match_by_name(df, prof, norm, unusable, basin, sub_coords)
    if cm is None:
        print("no coordinates in this file; name is the only key")
    if nm is None:
        print("no name column in this file; coordinates are the only key")
    if cm is not None and nm is not None:
        j = cm.merge(nm, on="base_id", suffixes=("_coord", "_name"))
        gc = j.substation_name_coord.notna()
        gn = j.substation_name_name.notna()
        # A bus BOTH routes leave unmapped is not a disagreement -- it is the
        # two routes agreeing. Comparing the raw columns would call it one,
        # because NaN != NaN.
        both = gc & gn
        agree = both & (j.substation_name_coord == j.substation_name_name)
        conflict = both & (j.substation_name_coord != j.substation_name_name)
        only_c, only_n = gc & ~gn, gn & ~gc
        neither = ~gc & ~gn
        print(f"both routes resolved, SAME substation : {int(agree.sum()):>6,}")
        print(f"both routes resolved, DIFFERENT one   : {int(conflict.sum()):>6,}"
              f"   <- coords route differs (NOT used)")
        print(f"only coordinates resolved             : {int(only_c.sum()):>6,}")
        print(f"only the name resolved                : {int(only_n.sum()):>6,}")
        print(f"neither resolved (unmapped, as intended): {int(neither.sum()):>5,}")
        dis = j[conflict]
        if len(dis):
            print("")
            print(f"the {len(dis)} case(s) where the two routes differ, first "
                  f"10. The NAME answer is the one used; the coordinate answer "
                  f"is shown only to make the difference visible. Nearly every "
                  f"one of these is the coordinate route landing on a different "
                  f"nearby station, which is precisely why it is not the "
                  f"default -- no action is needed unless the NAME answer looks "
                  f"wrong:")
            sub = coords.set_index(["utility", "substation_name"])
            for r in dis.head(10).itertuples():
                row = df[df.base_id == r.base_id].iloc[0]
                extra = ""
                if (r.utility_name, r.substation_name_name) in sub.index:
                    c = sub.loc[(r.utility_name, r.substation_name_name)]
                    d = hav_km(row[LAT_COL], row[LON_COL], c.util_lat, c.util_lon)
                    extra = f", {float(d):.1f} km from the row's coordinates"
                print(f"  bus {r.base_id} named {row.get(NAME_COL)!r}")
                print(f"    coords -> {r.utility_coord}/"
                      f"{r.substation_name_coord} at {r.coord_dist_km:.3f} km")
                print(f"    name   -> {r.utility_name}/"
                      f"{r.substation_name_name}{extra}")
        print()
        print("Match order is load_station -> station_name -> (coords, off by")
        print("default). Coordinates are NOT used, so a difference above is")
        print("information about an unused route, not a defect. SDG&E")
        print("coordinates in particular are imprecise -- a median 1.3 km even")
        print("on exact name matches -- so a 2-4 km gap there is normal.")
        print("'only coordinates resolved' is expected too: those are buses you")
        print("did not name, and matching them on proximity would override that.")
    print()
    print("VERDICT")
    sm = match_by_station(df, prof)
    n_bus = df.base_id.nunique()
    st_ok = set()
    if sm is not None:
        st_ok = set(sm[sm.station_status == "ok"].base_id)
        print(f"  load_station (rule 0): {len(st_ok):,}/{n_bus:,} buses resolved")
    if nm is not None:
        nm_ok = set(nm[nm.name_status.str.startswith("ok")].base_id)
        print(f"  name fallback:         "
              f"{len(nm_ok - st_ok):,} more, {len(nm_ok):,} on its own")
    if cm is not None:
        print(f"  coords (NOT used):     {int(cm.coord_ok.sum()):,} would "
              f"resolve, median {cm.coord_dist_km.median():.3f} km")
    final = st_ok | (set(nm[nm.name_status.str.startswith("ok")].base_id)
                     if nm is not None else set())
    print(f"  => MAPPED:             {len(final):,}/{n_bus:,} buses")
    print("     unmapped, by reason (bus counts) -- these are the buses that "
          "end up carrying the statewide shape:")
    # reasons for the buses that are actually unmapped, not for every route miss
    unmapped = set(df.base_id) - final
    reasons = []
    if sm is not None:
        rep = sm[sm.base_id.isin(unmapped)]
        nmf = (nm.set_index("base_id").name_status.to_dict()
               if nm is not None else {})
        for st, k in rep.station_status.value_counts().items():
            if st != "no load_station supplied":
                reasons.append((st, k))
        blank = rep[rep.station_status == "no load_station supplied"].base_id
        sub = pd.Series([nmf.get(b, "no name supplied") for b in blank])
        reasons += list(sub.value_counts().items())
    elif nm is not None:
        reasons = [(st, k) for st, k in nm.name_status[
            nm.base_id.isin(unmapped)].value_counts().items()]
    for st, k in sorted(reasons, key=lambda t: -t[1]):
        print(f"      {k:>6,}  {st}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True,
                    help="the NODE UNIVERSE: every node you want load on. Rows "
                         "the mapping does not cover simply leave `name` and "
                         "`lat`/`long` blank.")
    ap.add_argument("--mapping-input", default=None,
                    help="OPTIONAL second file carrying the match info for a "
                         "SUBSET of buses (`bus_id` plus `name` and/or "
                         "`lat`/`long`, `utility`). Joined onto the universe by "
                         "bus number. Use this when your mapping is a separate, "
                         "smaller file -- do NOT pass it as --input, or the "
                         "unmapped buses vanish from the allocation.")
    ap.add_argument("--expect-buses", type=int, default=None,
                    help="refuse unless the universe has exactly this many "
                         "buses. The cheapest guard against silently handing in "
                         "the mapping file instead of the universe.")
    ap.add_argument("--expect-nodes", type=int, default=None,
                    help="refuse unless the universe has exactly this many "
                         "sub-nodes")
    ap.add_argument("--out", default=None,
                    help="directory for nodes.csv + mapping.csv")
    ap.add_argument("--match", choices=MATCH_MODES, default="name",
                    help="how to build the edge list. Default `name`: in a "
                         "real bus list `name` is the CURATED match (blank when "
                         "the bus was not matched), so `coords` would override "
                         "that decision by mapping blank-name rows anyway.")
    ap.add_argument("--max-coord-dist-km", type=float, default=5.0,
                    help="a coordinate match further than this is not used")
    ap.add_argument("--max-name-dist-km", type=float, default=25.0,
                    help="a name match this far from the row's own coordinates "
                         "is reported as a disagreement")
    ap.add_argument("--drop-cols", default="",
                    help="comma-separated columns to delete before anything "
                         "else -- for the 'what can we do without' experiment")
    ap.add_argument("--audit", action="store_true",
                    help="print the column audit and the key comparison, "
                         "write nothing")
    args = ap.parse_args()

    path = Path(args.input)
    df = read_input(path)
    if args.drop_cols:
        drop = [c.strip() for c in args.drop_cols.split(",") if c.strip()]
        missing = [c for c in drop if c not in df.columns]
        if missing:
            raise SystemExit(f"--drop-cols names absent column(s) {missing}")
        df = df.drop(columns=drop)
        print(f"dropped {drop}")

    n_bus_universe = df.base_id.nunique()
    print(f"universe: {len(df):,} sub-node(s) in {n_bus_universe:,} bus(es)")
    if args.expect_buses is not None and n_bus_universe != args.expect_buses:
        raise SystemExit(
            f"--expect-buses {args.expect_buses} but the universe has "
            f"{n_bus_universe}. If this is far too small, you probably passed "
            f"the MAPPING file as --input; pass the full node universe as "
            f"--input and the mapping as --mapping-input.")
    if args.expect_nodes is not None and len(df) != args.expect_nodes:
        raise SystemExit(f"--expect-nodes {args.expect_nodes} but the universe "
                         f"has {len(df)} sub-node(s)")

    # ---- optional separate mapping file, joined on by bus ----------------
    if args.mapping_input:
        mpath = Path(args.mapping_input)
        mp = pd.read_csv(mpath)
        if BUS_COL not in mp.columns:
            raise SystemExit(f"{mpath}: --mapping-input needs {BUS_COL!r}")
        mp["base_id"] = mp[BUS_COL].astype(str).str.strip()
        keys = [c for c in (NAME_COL, LAT_COL, LON_COL, UTIL_COL, KV_COL)
                if c in mp.columns]
        if not any(c in keys for c in (NAME_COL, LAT_COL)):
            raise SystemExit(
                f"{mpath}: --mapping-input carries no join key; it needs "
                f"{NAME_COL!r} and/or {LAT_COL!r}+{LON_COL!r}")
        mp = mp[["base_id", *keys]].drop_duplicates("base_id")
        unknown = set(mp.base_id) - set(df.base_id)
        if unknown:
            raise SystemExit(
                f"{mpath}: {len(unknown)} bus(es) in the mapping are ABSENT "
                f"from the node universe, e.g. {sorted(unknown)[:5]}. A bus "
                f"you are not disaggregating to cannot be mapped -- either the "
                f"universe is incomplete (did you pass the mapping file as "
                f"--input?) or the two files disagree on bus numbering.")
        # the universe's own key columns are replaced where the mapping has a
        # value, so the mapping is authoritative about matching and the
        # universe stays authoritative about levels
        df = df.drop(columns=[c for c in keys if c in df.columns])
        df = df.merge(mp, on="base_id", how="left")
        for c in (NAME_COL, UTIL_COL):
            if c in df.columns:
                v = df[c].astype("string").fillna("").str.strip()
                df[c] = v.mask(v.str.lower().isin(
                    ["nan", "none", "null", "na", "n/a", "-"]), "")
        print(f"mapping-input: {len(mp):,} bus(es) carry match info "
              f"({len(mp) / n_bus_universe * 100:.1f}% of the universe)")

    prof, coords = load_fleet()
    unusable = unusable_envelopes()
    basin = basin_route(prof, norm)
    sub_coords = {(r.utility, r.substation_name): (r.util_lat, r.util_lon)
                  for r in coords.itertuples()}

    if args.audit:
        audit(df, prof, coords, args, norm, unusable, basin, sub_coords)
        return

    cm = match_by_coords(df, coords, args.max_coord_dist_km)
    nm = match_by_name(df, prof, norm, unusable, basin, sub_coords)
    sm = match_by_station(df, prof)

    # Rule 0: `load_station` is a KEY and supersedes both other routes where it
    # is present. The name route stays on as the fallback for blanks -- it
    # recovers the few the upstream join leaves out (e.g. `Balch 1` ->
    # `pge/BALCH NO 1`, a known 1.3 km name/coordinate ambiguity).
    st_edges = None
    if sm is not None:
        st_edges = sm[sm.station_status == "ok"][
            ["base_id", "substation_name", "utility"]]
        print(f"load_station: {len(st_edges):,} of "
              f"{df.base_id.nunique():,} bus(es) resolved by key")
        bad = sm[(sm.station_status != "ok")
                 & (sm.station_status != "no load_station supplied")]
        for stt, k in bad.station_status.value_counts().items():
            print(f"    {k:>6,}  {stt[:88]}")
        # Validate rather than guess: a bus both routes answer should answer the
        # same. Zero such conflicts today, so any that appear are a regression.
        if nm is not None:
            j = st_edges.merge(
                nm[nm.name_status.str.startswith("ok")][
                    ["base_id", "substation_name", "utility"]],
                on="base_id", suffixes=("_station", "_name"))
            conflict = j[(j.substation_name_station != j.substation_name_name)
                         | (j.utility_station != j.utility_name)]
            if len(conflict):
                print(f"  WARNING {len(conflict)} bus(es) where load_station "
                      f"and the name route DISAGREE -- load_station wins, but "
                      f"this is a regression signal:")
                for r in conflict.head(10).itertuples():
                    print(f"    bus {r.base_id}: load_station -> "
                          f"{r.utility_station}/{r.substation_name_station}"
                          f" vs name -> {r.utility_name}/"
                          f"{r.substation_name_name}")
            else:
                print(f"    {len(j):,} bus(es) answered by both routes; "
                      f"0 disagreements")

    if args.match == "coords":
        if cm is None:
            raise SystemExit(
                f"--match coords needs {LAT_COL} and {LON_COL}; this file has "
                f"neither. Use --match name (and keep '{UTIL_COL}', because a "
                f"shared name cannot be resolved without coordinates).")
        edges = cm[cm.coord_ok][["base_id", "substation_name", "utility"]]
        src = "coords"
    elif args.match == "name":
        if nm is None:
            raise SystemExit(f"--match name needs a {NAME_COL} column")
        edges = nm[nm.name_status.str.startswith("ok")][["base_id", "substation_name",
                                            "utility"]]
        src = "name"
    else:
        if cm is None and nm is None:
            raise SystemExit("no join key at all in this file")
        keep = cm[cm.coord_ok][["base_id", "substation_name", "utility"]] \
            if cm is not None else pd.DataFrame(columns=["base_id", "substation_name", "utility"])
        if nm is not None:
            fill = nm[(nm.name_status.str.startswith("ok"))
                      & (~nm.base_id.isin(keep.base_id))]
            keep = pd.concat([keep, fill[["base_id", "substation_name",
                                          "utility"]]], ignore_index=True)
        edges, src = keep, "coords-then-name"

    # rule 0 wins wherever it answered; the selected route only fills the rest
    if st_edges is not None and len(st_edges):
        extra = edges[~edges.base_id.isin(st_edges.base_id)]
        n_fill = len(extra)
        edges = pd.concat([st_edges, extra], ignore_index=True)
        src = f"load_station (+{n_fill:,} from {src})"

    # Coordinates are NOT a match route under the default, so a coords-vs-name
    # difference says something about the route that was NOT used. It is
    # reported as information, never as a warning: nearly every such case is
    # the coordinate route picking a different nearby station, which is the
    # documented reason it is not the default.
    if cm is not None and nm is not None and not args.match.startswith("coords"):
        j = cm.merge(nm, on="base_id", suffixes=("_coord", "_name"))
        dis = j[(j.substation_name_coord != j.substation_name_name)
                & j.substation_name_name.notna()
                & j.substation_name_coord.notna()]
        if len(dis):
            print(f"  note: on {len(dis):,} mapped bus(es) the UNUSED "
                  f"coordinate route would have picked a different station. "
                  f"That is expected -- proximity is not identity -- and "
                  f"nothing is wrong with those buses. --audit lists them.")

    nodes = df[["node_id", "base_id", "subname", "winter_load", "summer_load"]].copy()
    # LABEL_COL is deliberately NOT carried: nothing downstream uses it
    for c in (NAME_COL, UTIL_COL, KV_COL, LAT_COL, LON_COL, "source"):
        if c in df.columns:
            nodes[f"src_{c.strip(chr(39))}"] = df[c].to_numpy()

    out = Path(args.out) if args.out else path.parent / (path.stem + "_approach3")
    out.mkdir(parents=True, exist_ok=True)
    nodes.to_csv(out / "nodes.csv", index=False)
    edges.to_csv(out / "mapping.csv", index=False)
    n_bus = df.base_id.nunique()
    print(f"wrote {out}")
    print(f"  nodes.csv    {len(nodes):,} sub-nodes in {n_bus:,} buses")
    print(f"  mapping.csv  {len(edges):,} edges by {src} "
          f"({len(edges) / n_bus * 100:.1f}% of buses mapped)")

    # ---- coverage, stated explicitly -------------------------------------
    # The mapped/unmapped split decides which nodes carry a MEASURED hourly
    # shape and which carry the statewide one, so it is never left implicit.
    mapped_b = set(edges.base_id)
    is_m = df.base_id.isin(mapped_b)
    gross = np.abs(df[list(LEVEL_COLS)].to_numpy()).sum()
    print(f"\n  COVERAGE")
    print(f"    buses mapped        {len(mapped_b):>7,} of {n_bus:,} "
          f"({len(mapped_b) / n_bus * 100:5.1f}%)")
    print(f"    sub-nodes mapped    {int(is_m.sum()):>7,} of {len(df):,} "
          f"({is_m.sum() / len(df) * 100:5.1f}%)")
    if gross > 0:
        ms = np.abs(df.loc[is_m, list(LEVEL_COLS)].to_numpy()).sum() / gross
        print(f"    level mass mapped   {ms * 100:>7.2f}%  <- the share of load "
              f"whose HOURLY SHAPE is measurement-driven")
        print(f"    level mass unmapped {(1 - ms) * 100:>7.2f}%  <- carries the "
              f"STATEWIDE hourly shape, scaled by its own level")
    print(f"    100% of load is allocated either way; what the mapping changes "
          f"is only the shape.")
    # Reasons are reported ONLY for buses that ended up unmapped. A bus the
    # name route failed on but `load_station` resolved is mapped, and listing
    # its name-route failure here would read as a problem that is not one.
    unmapped = set(df.base_id) - mapped_b
    if sm is not None:
        rep = sm[sm.base_id.isin(unmapped)]
        counts = rep.station_status.value_counts()
        nmf = (nm.set_index("base_id").name_status.to_dict()
               if nm is not None else {})
        blank = rep[rep.station_status == "no load_station supplied"].base_id
        why = [(st, k) for st, k in counts.items()
               if st != "no load_station supplied"]
        # a blank load_station fell through to the name route, so its reason is
        # that route's
        sub = pd.Series([nmf.get(b, "no name supplied") for b in blank])
        why += list(sub.value_counts().items())
    elif src.startswith("coords"):
        why = [(st, k) for st, k in report_unmapped(cm, "coords")]
    else:
        why = [(st, k) for st, k in report_unmapped(nm, "name")]
    for status, k in sorted(why, key=lambda t: -t[1]):
        print(f"    {k:>7,}  {status}")
    if len(edges) == n_bus:
        print(f"  NOTE every bus is mapped, so there is no slack basis and")
        print(f"       --conserve slack will refuse. Use --conserve renorm,")
        print(f"       or accept that with one shared shape per bus the shape")
        print(f"       cancels and the result is y(t) x the level share.")


if __name__ == "__main__":
    main()
