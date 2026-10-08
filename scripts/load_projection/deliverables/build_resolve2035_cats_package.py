"""Build a shareable package of CA-wide 2035 hourly load at every CATS bus.

Takes the RESOLVE forecast for a chosen model year (`--year`, default 2035) for
**all six California zones** (PGE, SCE, SDGE, IID, LDWP, NCNC), nets out BTM PV at
that year's capacities, and allocates the
CA-wide hourly series across CATS buses with the county-first allocation that
already exists in `genx/rescale_genx_demand.py`. Output under
`deliverables/resolve_2035_cats_nodal/`: hourly wide CSVs + reference tables +
a standalone README.

Nothing here is a new method and nothing here reimplements one. The allocation
functions are IMPORTED from `rescale_genx_demand` (`candidate_buses`,
`county_first_shares`, `expand_shares_to_cells`, `stoch_pool_shares`,
`expand_stoch_shares_to_cells`) so this script cannot drift from the pipeline.
What it adds is (a) a CA-wide 2035 target and (b) packaging.

Why county-first, and what it fixes
-----------------------------------
An earlier version of this package disaggregated only PGE/SCE/SDGE to their
metered substations and mapped those to their nearest bus. That discarded the
~19% of CA-wide net load in IID/LDWP/NCNC and left 58% of the buses CATS itself
loads at zero -- Sacramento 98.7% empty, Stanislaus 94.6%. County-first fixes
both: ReEDS county WEIGHTS set each county's share of the state, and inside a
county the `--alpha ratio` split gives the uncovered buses an equal share and
the substation buses the rest by max-load envelope weight. Uncovered buses need
no substation profile, which is exactly what fills LADWP / SMUD / IID territory.

    w_c              = R_c / sum_j R_j          ReEDS county shares, NEVER levels
    uncovered bus    = alpha * w_c / u          equal split,  alpha = u/n
    substation bus i = (1-alpha) * w_c * e_i / sum e   within-county weight

Every candidate bus carries load: 3,768 of the 3,769 in the pool. Note the
uncovered split `alpha * w_c / u` is PER COUNTY, scaled by that county's ReEDS
share. That is the structural fix relative to the stochastic-pool family, whose
`beta / n_unc` is a single STATEWIDE constant with no county grouping: it
distributes by bus count and starves any county below the gate (Sacramento).

Target construction (2035, net of BTM, all six zones)
----------------------------------------------------
RESOLVE ships one 8,760-hour shape per weather year 2000-2022, each
independently rescaled to the SAME annual energy -- so annual TWh is
weather-invariant, only the shape moves, and every weather year is 8,760 hours
(Feb 29 dropped), so 2035 needs no leap-year handling. The 2035 level is
RESOLVE's own `scale_multiplier` logic at a different model year:

    gross_2035(t) = demand_mw_2024scaled(t) x (E_2035 / E_2024)      per zone
    btm_2035(t)   = btm_pv_mw(t) x (cap_2035 / cap_2024)             per zone
    net_2035(t)   = gross_2035(t) - btm_2035(t)

`E_y` = `annual_energy_forecast` from `interim/loads/{ZONE}_Baseline*.csv`;
`cap_y` = `Customer_PV` `planned_capacity` under `2024_IEPR_Local_Reliability`.
`resolve_hourly_profiles.csv` already stores `btm_pv_mw = weather_factor x
cap_2024`, so rescaling by the capacity ratio recovers `weather_factor x
cap_2035` exactly. `demand_mw_net` is non-null for all six zones.

RESOLVE sets the LEVEL; ReEDS contributes normalized county SHARES only. ReEDS
load levels are never read (standing rule). Timestamps keep the weather year's
own calendar -- relabelling to 2035 would misalign weekday/weekend -- and
`hour_of_year` (1-8760) is the year-agnostic key.

Variants written
----------------
Every variant is county-first at `--alpha ratio`, `--level monthhour` (all 288
cells), and on NO axis does a county total move: the ReEDS county shares, alpha =
u/n and the equal pool are identical throughout. The axes only re-split each
county's envelope pool among its covered buses. Shares sum to exactly 1 per cell
and are non-negative, so the CA-wide hourly total is conserved by construction and
no bus-hour is ever negative.

WITHIN-COUNTY WEIGHT SOURCE (`county_weights`, dispatched by
`rescale_genx_demand.county_weight_src`):
  envelope     mean `max_load` envelope per substation -- the published default.
  stochastic   Approach 2's own output (`substation_annual` + `substation_to_node`
               for the static weight, `stoch_cell_weights` per cell). Rule-clean:
               the envelope was preferred over Approach 1's MWh to avoid counting
               ReEDS twice, and Approach 2 descends from the utility envelopes plus
               the target series, never from ReEDS. Shipped as the draw mean plus
               individual draws (`--draws`), because the mean correlates ~0.996
               with the envelope variant at ANNUAL level -- the visible stochastic
               signal is in per-cell shape and draw-to-draw spread, not annual
               energy. Static stochastic weights are refused upstream (they
               collapse the model to a rescaled envelope midpoint), which is why
               every variant here is month-hour.

UNCOVERED-POOL SPLIT (`uncovered`, dispatched by
`rescale_genx_demand.uncovered_src`) -- the LARGEST axis, because the uncovered
pool carries ~51% of CA-wide load:
  catsprop     each uncovered bus takes its county's uncovered pool in proportion
               to its OWN load in CATS's demand table. CATS does not treat those
               buses as interchangeable (median CV 0.79 within county), so this
               keeps information a flat split throws away. The deliverable default.
  equalsplit   flat `alpha * w_c / u`. Always written once as a comparison file.
  Switching between them moves 19.17% of CA-wide load; the pool SIZE and every
  county total are identical either way.

NODAL MAP:
  prox         `substation_node_map.csv` (nearest demand bus)
  voltres      `substation_node_map__voltrestrict.csv` (bus voltage class must
               match the substation's high-side class)

`stoch_variant()` (the `stoch_pool_shares` pool-and-redistribute family) is kept
in this module for the section-H diagnostic ONLY and is deliberately never
written: it is structurally a HOLD method needing a control to copy through, which
a fresh 2035 projection has not got, so its county totals collapse in muni
territory at every gate setting that does not degenerate to a bus-count split.

CLI parameters
  --year           RESOLVE model year to project, 2024-2045 (default 2035). Only
                   the target builder is year-specific; the allocation is not.
  --load-basis     net (DEFAULT) = disaggregate load net of BTM PV, the consistent
                   pairing with net-of-meter substation weights. gross = allocate
                   RESOLVE's gross load, which puts the BTM offset onto the
                   substations; only for a caller who wants the gross series.
  --overlays       all (DEFAULT) = RESOLVE's actual load, Baseline + every overlay.
                   none = Baseline ONLY (CA-wide 2035 gross 327.35 TWh vs 445.76),
                   which drops all electrification and data-centre growth. Provided
                   for callers who want that specific series to test against; it is
                   NOT RESOLVE's forecast and must not be presented as one.
  --bus-types      all (DEFAULT) = Substation buses + the AddedNodes CATS loads.
                   substation = physical substations only, every AddedNode unloaded
                   (including the 607 CATS loads). County totals are unchanged
                   either way; the dropped load is re-split within its own county.
  --weather-year   RESOLVE weather year for the hourly shape (default: the
                   median CA-wide-net-annual-peak year of the 23, printed)
  --county-year    year of the ReEDS county table whose NORMALIZED shares set
                   w_c (default 2023, the latest available; only shares are
                   used, never levels)
  --uncovered      cats (DEFAULT) splits a county's uncovered pool by each bus's
                   own CATS demand; equal splits it flat. Whichever is NOT chosen
                   is still written once as a comparison file.
  --pool           cats_loaded (DEFAULT) places load only on buses CATS itself
                   loads, plus any bus the chosen map reaches by a direct name
                   match; `all` is the GenX pipeline's wider 2026-08-12 pool,
                   which also loads Type='Substation' buses CATS leaves at zero
  --maps           comma-separated map axis (default prox,voltres); the
                   stochastic-weight variants use the FIRST map only
  --draws          Approach-2 draw indices shipped alongside the draw mean
                   (default "0,1"; empty ships the mean only)
  --stochastic-run Approach 2 run folder for the stochastic weight source
                   (default: the CATS-calibrated run, as in the GenX rescaler)
  --decimals       printed precision of the hourly files (default 1, matching
                   CATS's own Demand_data.csv grid)
  --readme         README template to copy in (default the one beside this script);
                   override for a non-default run whose numbers differ
  --out            package root (default deliverables/resolve_2035_cats_nodal)
  --no-zip         skip writing the .zip alongside the folder

Outputs (under --out)
  README.md                                  copied from
                                             README_resolve_2035_cats_nodal.md
                                             next to this script -- edit it
                                             there, not in the package
  nodal_hourly/countyfirst_{weights}__{map}.csv.gz  8,760 rows x ~3,768 cols,
                                             `Demand_MW_z{bus_i}` (CATS/GenX
                                             column convention)
  summary/variant_summary.csv                per variant: TWh, peak, buses,
                                             conservation error, file size
  summary/node_annual_mwh.csv                per bus, one column per variant
  summary/node_peak_mw.csv                   per bus, one column per variant
  summary/county_allocation.csv              per county: ReEDS share, bus counts,
                                             alpha, equal/envelope pool split
  reference/resolve_2035_statewide.csv       hourly baseline/overlay/gross/btm/net
                                             per RESOLVE zone
  reference/resolve_2035_scaling.csv         per-zone baseline ratio, overlay and
                                             gross energy for the target year
  reference/resolve_2035_components.csv      every overlay component's energy, with
                                             the RESOLVE scenario each came from
  reference/cats_node_metadata.csv           bus_i, kV, Type, Lat, Lon, county
  reference/node_shares_static.csv           each bus's period-average share
  reference/substation_node_map__{map}.csv   copies of the maps used
  code/build_resolve2035_cats_package.py     this script
  code/deliverable_numbers.py                recompute script for every measured
                                             figure quoted in the README

Usage
  python scripts/load_projection/deliverables/build_resolve2035_cats_package.py
  python scripts/load_projection/deliverables/build_resolve2035_cats_package.py \
      --weather-year 2022 --draws 0,1,2
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import sys
import zipfile
from argparse import Namespace
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/genx"))

import rescale_genx_demand as RS  # noqa: E402
from genx_demand_io import round_to_printed  # noqa: E402
from load_projection.periods import (  # noqa: E402
    CONTINUOUS,
    SNAPSHOTS,
    build_plan,
)
from load_projection.stochastic import cell_index  # noqa: E402

RESOLVE_RAW = (ROOT / "data/raw/RESOLVE Code Base and Inputs"
               / "RESOLVE Code Base and Inputs" / "data")
RESOLVE_HOURLY = ROOT / "data/processed/resolve/resolve_hourly_profiles.csv"
NODAL_DIR = ROOT / "data/processed/load_projection/nodal/CATS"
CATS_BUSES = ROOT / "data/raw/CATS/CATS_buses.csv"
DEFAULT_OUT = ROOT / "deliverables/resolve_2035_cats_nodal"
README_SRC = Path(__file__).with_name("README_resolve_2035_cats_nodal.md")

# all six RESOLVE California zones -- NOT just the three IOUs
ZONES = ["PGE", "SCE", "SDGE", "IID", "LDWP", "NCNC"]
IOUS = ["PGE", "SCE", "SDGE"]
DEFAULT_TARGET_YEAR = 2035   # overridable with --year; RESOLVE covers 2024-2045
BASE_YEAR = 2024             # the year demand_mw_2024scaled is scaled to
BTM_SCENARIO = "2024_IEPR_Local_Reliability"
N_CELLS = 288
MIN_DRAWS = 3          # refuse a 'mean' built from too few Monte Carlo draws
ALL_CELLS = {(m, h) for m in range(1, 13) for h in range(24)}

# The Baseline component per zone. RESOLVE's load is Baseline PLUS additive
# overlays (see build_target); the CHP_Retire twin carries identical annual energy
# and is skipped so it cannot be double counted.
INTERIM_LOAD_FILES = {
    "PGE": "PGE_Baseline_CHP_Not_Retire.csv",
    "SCE": "SCE_Baseline_CHP_Not_Retire.csv",
    "SDGE": "SDGE_Baseline_CHP_Not_Retire.csv",
    "IID": "IID_Baseline.csv",
    "LDWP": "LDWP_Baseline_CHP_Not_Retire.csv",
    "NCNC": "NCNC_Baseline_CHP_Not_Retire.csv",
}

# Scenario for the overlay components that publish one (AAEE, AAFS, Data_Centers,
# Storage_Losses); the rest carry a single "2024_IEPR" scenario. Matches the
# Customer_PV scenario used for BTM, so the whole target is one scenario.
OVERLAY_SCENARIO = "2024_IEPR_Local_Reliability"
HOURS_PER_YEAR = 8760

MAP_LABELS = {"prox": "substation_node_map.csv",
              "voltres": "substation_node_map__voltrestrict.csv"}


# ---------------------------------------------------------------------------
# 1. CA-wide RESOLVE 2035 target
# ---------------------------------------------------------------------------

def _annual_energy(zone: str) -> pd.Series:
    df = pd.read_csv(RESOLVE_RAW / "interim/loads" / INTERIM_LOAD_FILES[zone])
    rows = df[df.attribute == "annual_energy_forecast"].copy()
    rows["year"] = pd.to_datetime(rows.timestamp).dt.year
    rows["mwh"] = pd.to_numeric(rows.value, errors="coerce")
    return rows.set_index("year")["mwh"]


def _component_annual(path: Path, scenario: str) -> tuple[pd.Series, str]:
    """(year -> MWh, scenario used) for one RESOLVE load component."""
    df = pd.read_csv(path)
    a = df[df.attribute == "annual_energy_forecast"].copy()
    a["year"] = pd.to_datetime(a.timestamp).dt.year
    a["mwh"] = pd.to_numeric(a.value, errors="coerce")
    scens = list(a.scenario.dropna().unique())
    pick = scenario if scenario in scens else (scens[0] if scens else None)
    a = a[a.scenario == pick] if pick else a
    if a.year.duplicated().any():
        raise ValueError(f"{path.name}: duplicate years for scenario {pick!r}")
    return a.set_index("year")["mwh"], str(pick)


def _component_profile_path(path: Path, scenario: str) -> Path:
    """The hourly profile RESOLVE points this component at, for `scenario`."""
    df = pd.read_csv(path)
    pr = df[df.attribute.isin(["profile_model_years", "profile"])].copy()
    scens = list(pr.scenario.dropna().unique())
    pick = scenario if scenario in scens else (scens[0] if scens else None)
    row = pr[pr.scenario == pick]
    if row.empty:
        raise ValueError(f"{path.name}: no profile for scenario {pick!r}")
    out = RESOLVE_RAW / str(row.value.iloc[0])
    if not out.exists():
        raise FileNotFoundError(f"{path.name} points at a missing profile: {out}")
    return out


def _overlay_hourly(zone: str, target_year: int, scenario: str
                    ) -> tuple[np.ndarray, pd.DataFrame]:
    """(8,760 MW overlay total for `target_year`, per-component breakdown).

    Overlay profiles are indexed by MODEL YEAR 2024-2050 and are already at the
    level RESOLVE uses -- their hours sum to `annual_energy_forecast` exactly
    (asserted below). There is nothing to project: the year is selected, not
    derived. They carry no weather dimension, so the same vector applies to every
    weather year.
    """
    total = np.zeros(HOURS_PER_YEAR, dtype=np.float64)
    rows = []
    for f in sorted((RESOLVE_RAW / "interim/loads").glob(f"{zone}_*.csv")):
        if "CHP_Retire" in f.name or f.name == INTERIM_LOAD_FILES[zone]:
            continue                      # the Baseline is handled separately
        energy, used = _component_annual(f, scenario)
        if target_year not in energy.index:
            raise ValueError(f"{f.name}: no annual_energy_forecast for {target_year}")
        ppath = _component_profile_path(f, scenario)
        prof = pd.read_csv(ppath)
        # RESOLVE is inconsistent: overlay profiles head their time column
        # "timestamp", the Baseline profiles "datetime"
        tcol = next((c for c in ("timestamp", "datetime") if c in prof.columns), None)
        if tcol is None or "profile_model_years" not in prof.columns:
            raise ValueError(f"{ppath.name}: unexpected columns {list(prof.columns)}")
        prof[tcol] = pd.to_datetime(prof[tcol])
        yr = prof[prof[tcol].dt.year == target_year]
        if len(yr) != HOURS_PER_YEAR:
            raise ValueError(f"{f.name}: model year {target_year} has {len(yr)} hours, "
                             f"expected {HOURS_PER_YEAR}")
        mw = pd.to_numeric(yr.profile_model_years, errors="coerce").to_numpy(float)
        # the profile must already BE the model year's load, not a shape to scale
        got, want = mw.sum(), float(energy[target_year])
        if want != 0 and abs(got / want - 1.0) > 1e-4:
            raise ValueError(
                f"{f.name}: profile sums to {got/1e6:.4f} TWh but "
                f"annual_energy_forecast says {want/1e6:.4f} TWh -- this component "
                f"is NOT pre-levelled; it would need scale_by_energy treatment")
        total += mw
        rows.append({"zone": zone, "component": f.name[len(zone) + 1:-4],
                     "scenario": used, "twh": got / 1e6})
    return total, pd.DataFrame(rows)


def _btm_capacity(zone: str, year: int) -> float:
    df = pd.read_csv(RESOLVE_RAW / "interim/resources" / f"{zone}_Customer_PV.csv")
    rows = df[(df.attribute == "planned_capacity") & (df.scenario == BTM_SCENARIO)].copy()
    rows["year"] = pd.to_datetime(rows.timestamp).dt.year
    hit = rows[rows.year == year]
    if hit.empty:
        raise ValueError(f"{zone}: no planned_capacity for {year}/{BTM_SCENARIO}")
    return float(hit.value.iloc[0])


def cell_frame_index() -> range:
    """The 288 (month, hour_pst) cells the share matrices are indexed by."""
    return range(12 * 24)


def available_model_years() -> list[int]:
    """Model years RESOLVE's Baseline carries in EVERY zone.

    The Baseline's `annual_energy_forecast` is the binding constraint: the
    overlays reach 2050 but the Baseline -- the only weather-varying component --
    stops earlier, so a year past it could only be produced by projecting, which
    this builder will not do. Measured 2024-2045 for all six zones.
    """
    common: set[int] | None = None
    for zone in INTERIM_LOAD_FILES:
        yrs = {int(y) for y in _annual_energy(zone).dropna().index}
        common = yrs if common is None else (common & yrs)
    return sorted(common or ())


def available_weather_years() -> list[int]:
    """Weather years RESOLVE's Baseline carries (2000-2022), read from the
    processed profiles so this needs no assembled target."""
    d = pd.read_csv(RESOLVE_HOURLY, usecols=["datetime_pst"])
    return sorted(int(y) for y in pd.to_datetime(d.datetime_pst).dt.year.unique())


def resolve_plan(args):
    """The ordered (model year, weather year) jobs to build.

    With neither --period nor --weather-years/--weather-year, this reproduces the
    published behaviour exactly: the single --year, at the median-peak weather
    year, which needs the target assembled first to find. Everything else goes
    through the grammar in `load_projection.periods`.
    """
    avail_my, avail_wy = available_model_years(), available_weather_years()
    period = args.period or str(args.year)
    wy_spec = args.weather_years or (str(args.weather_year)
                                     if args.weather_year else None)
    if wy_spec:
        return build_plan(period, wy_spec, avail_my, avail_wy), None

    from load_projection.periods import parse_period
    mode, payload, _ = parse_period(period)
    if mode != SNAPSHOTS or len(payload) != 1:
        raise SystemExit(
            "--weather-years is required for a multi-year period; the "
            "median-peak default only applies to a single model year. The "
            f"source has {avail_wy[0]}-{avail_wy[-1]}.")
    my = payload[0]
    if my not in avail_my:
        raise SystemExit(f"model year {my} is not in the source, which has "
                         f"{avail_my[0]}-{avail_my[-1]}")
    target, scaling = build_target(my, OVERLAY_SCENARIO, args.overlays)
    wy = median_peak_weather_year(target, args.load_basis)
    print(f"  weather year: {wy} (median CA-wide {args.load_basis} annual peak "
          f"of {len(avail_wy)})")
    return (build_plan(str(my), str(wy), avail_my, avail_wy),
            (my, target, scaling))


def build_target(target_year: int = DEFAULT_TARGET_YEAR,
                 overlay_scenario: str = OVERLAY_SCENARIO,
                 overlays: str = "all") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Hourly RESOLVE gross/btm/net MW for all six CA zones, 23 weather years.

    RESOLVE's load is NOT the Baseline alone. It is the Baseline PLUS additive
    overlay components -- AAEE (negative, efficiency), AAFS (building
    electrification), Baseline/AATE LDVs and MHDVs (EVs), Data_Centers,
    Climate_Impacts, Storage_Losses -- each a separate file in `interim/loads/`
    with its own hourly profile. Omitting them understates CA-wide 2035 gross load
    by 118 TWh (327 vs 446), and they have their own shapes, so they cannot be
    approximated by scaling the Baseline.

    The two layers are constructed differently, which is the crux:

      Baseline   `profiles/loads/2024/{ZONE}_Baseline.csv` is a WEATHER-year shape
                 (2000-2022) carrying no level, so it is scaled by
                 E_base(target_year) / E_base(2024) -- RESOLVE's own
                 `scale_by_energy` logic. This is the only scaled quantity.
      Overlays   their profiles are indexed by MODEL YEAR 2024-2050 and are already
                 at RESOLVE's level: the hours of model year Y sum to that year's
                 `annual_energy_forecast` exactly (asserted per component). The
                 year is SELECTED, not projected. They have no weather dimension,
                 so one 8,760 vector applies to every weather year.

    `target_year` is any model year RESOLVE carries (2024-2045, limited by the
    Baseline's annual_energy_forecast). Only this function is source- and
    year-specific; everything downstream is agnostic.

    `overlays="none"` builds the BASELINE-ONLY target (CA-wide 2035 gross 327.35 TWh
    instead of 445.76). That is NOT RESOLVE's load -- it drops all electrification
    and data-centre growth -- and exists only because a caller wants that specific
    series, e.g. to exercise an algorithm against a known earlier number. Never the
    default, and never the right choice for a forecast someone will interpret.
    """
    cols = ["datetime_pst", "utility", "demand_mw_2024scaled", "btm_pv_mw"]
    r = pd.read_csv(RESOLVE_HOURLY, usecols=cols, parse_dates=["datetime_pst"])
    missing = set(ZONES) - set(r.utility.unique())
    if missing:
        raise ValueError(f"resolve_hourly_profiles.csv is missing zones {sorted(missing)}")
    r = r[r.utility.isin(ZONES)].copy()
    r["weather_year"] = r.datetime_pst.dt.year

    scale_rows, pieces, comp_rows = [], [], []
    for zone in ZONES:
        energy = _annual_energy(zone)
        if target_year not in energy.index:
            raise ValueError(
                f"{zone}: RESOLVE has no annual_energy_forecast for {target_year}; "
                f"available {int(energy.index.min())}-{int(energy.index.max())}")
        ratio = float(energy[target_year]) / float(energy[BASE_YEAR])
        cap_base = _btm_capacity(zone, BASE_YEAR)
        cap_tgt = _btm_capacity(zone, target_year)

        if overlays == "none":
            overlay = np.zeros(HOURS_PER_YEAR, dtype=np.float64)
            breakdown = pd.DataFrame(columns=["zone", "component", "scenario", "twh"])
        else:
            overlay, breakdown = _overlay_hourly(zone, target_year, overlay_scenario)
        comp_rows.append(breakdown)

        u = r[r.utility == zone].copy().sort_values("datetime_pst")
        u["hour_of_year"] = u.groupby("weather_year").cumcount() + 1
        if (u.groupby("weather_year").size() != HOURS_PER_YEAR).any():
            raise ValueError(f"{zone}: a weather year is not {HOURS_PER_YEAR} hours")
        u["baseline_mw"] = u.demand_mw_2024scaled * ratio
        u["overlay_mw"] = overlay[u.hour_of_year.to_numpy() - 1]
        u["gross_mw"] = u.baseline_mw + u.overlay_mw
        u["btm_mw"] = u.btm_pv_mw * (cap_tgt / cap_base)
        u["net_mw"] = u.gross_mw - u.btm_mw
        if u[["gross_mw", "btm_mw", "net_mw"]].isna().any().any():
            raise ValueError(f"{zone}: null hours in the target")
        pieces.append(u[["datetime_pst", "weather_year", "utility", "baseline_mw",
                         "overlay_mw", "gross_mw", "btm_mw", "net_mw"]]
                      .rename(columns={"utility": "zone"}))
        scale_rows.append({
            "zone": zone, "is_iou": zone in IOUS,
            "target_year": target_year,
            f"baseline_energy_{BASE_YEAR}_twh": energy[BASE_YEAR] / 1e6,
            "baseline_energy_target_twh": energy[target_year] / 1e6,
            "baseline_energy_ratio": ratio,
            "overlays": overlays,
            "overlay_energy_target_twh": overlay.sum() / 1e6,
            "gross_energy_target_twh": (energy[target_year] + overlay.sum()) / 1e6,
            f"btm_capacity_{BASE_YEAR}_mw": cap_base,
            "btm_capacity_target_mw": cap_tgt,
            "btm_capacity_ratio": cap_tgt / cap_base,
        })

    long = pd.concat(pieces, ignore_index=True)
    long = long.sort_values(["zone", "datetime_pst"]).reset_index(drop=True)
    long["hour_of_year"] = long.groupby(["zone", "weather_year"]).cumcount() + 1
    long = long.sort_values(["weather_year", "datetime_pst", "zone"])
    scaling = pd.DataFrame(scale_rows)
    scaling.attrs["components"] = pd.concat(comp_rows, ignore_index=True)
    return long.reset_index(drop=True), scaling


def median_peak_weather_year(target: pd.DataFrame, basis: str = "net") -> int:
    """Weather year whose CA-wide annual peak is the median of the 23, on `basis`."""
    tot = target.groupby(["weather_year", "datetime_pst"])[f"{basis}_mw"].sum()
    peaks = tot.groupby("weather_year").max().sort_values()
    return int(peaks.index[len(peaks) // 2])


def ca_series(target: pd.DataFrame, weather_year: int, basis: str = "net"
              ) -> tuple[pd.DataFrame, np.ndarray]:
    """(index frame with cell labels, CA-wide MW per hour) for one weather year.

    `basis="net"` (default) disaggregates load net of BTM PV, which is the
    consistent pairing: the substation weights are measured at the substation
    meter, downstream of rooftop solar. `basis="gross"` disaggregates RESOLVE's
    gross load instead -- the ~43 TWh BTM offset then lands on the substations, so
    it is only correct when a caller specifically wants the gross series.
    """
    col = f"{basis}_mw"
    t = target[target.weather_year == weather_year]
    y = (t.groupby(["datetime_pst", "hour_of_year"], as_index=False)[col].sum()
         .rename(columns={col: "y_mw"})
         .sort_values("datetime_pst").reset_index(drop=True))
    idx = y[["datetime_pst", "hour_of_year"]].copy()
    idx["month"] = idx.datetime_pst.dt.month
    idx["hour_pst"] = idx.datetime_pst.dt.hour
    idx["cell"] = cell_index(idx.month, idx.hour_pst)
    return idx, y.y_mw.to_numpy(dtype=np.float64)


# ---------------------------------------------------------------------------
# 2. Share matrices -- allocation IMPORTED, never reimplemented
# ---------------------------------------------------------------------------

def share_matrix(per_cell: pd.DataFrame, nodes: list[str]) -> np.ndarray:
    """Per-cell share table -> [288, n_nodes] dense matrix; asserts rows sum to 1."""
    pos = {n: i for i, n in enumerate(nodes)}
    S = np.zeros((N_CELLS, len(nodes)), dtype=np.float64)
    c = cell_index(per_cell.month.to_numpy(), per_cell.hour_pst.to_numpy())
    S[c, per_cell.node.map(pos).to_numpy()] = per_cell.share.to_numpy()
    rows = S.sum(axis=1)
    if not np.allclose(rows, 1.0, atol=1e-9):
        raise AssertionError(
            f"cell shares do not sum to 1: min {rows.min():.12f} max {rows.max():.12f}")
    return S


def county_first_variant(map_name: str, county_year: int, cache: dict,
                         weight_src: str = "envelope", draw: str = "mean",
                         run: str = "", pool: str = "cats_loaded",
                         uncovered: str = "cats", external: str | None = None,
                         bus_types: str = "all", system: str = "CATS"
                         ) -> tuple[list[str], np.ndarray, pd.DataFrame, dict]:
    """County-first shares for one (map, weight source, draw), expanded to 288 cells.

    `weight_src` selects the WITHIN-COUNTY weight only, through
    `rescale_genx_demand.county_weight_src`: "envelope" (mean max_load, the
    published default), "stoch" (Approach 2's per-substation output, per-cell in
    the expansion), or "external" (a measurement from outside this repo, built by
    `scripts/load_projection/external_loads/build_external_weights.py` into the
    folder named by `external`). The ReEDS county totals are identical in every
    case, so the variants differ only in how each county's measured pool is split
    among its covered buses.

    "external" differs from the other two in one way that matters: it can give a
    load to reference substations the utilities do not profile, so it also moves
    buses OUT of the uncovered pool. That raises `envelope_governed_share` -- the
    share of load whose within-county split is measurement-driven -- rather than
    merely re-splitting the same pool. Every variant still allocates 100% of load.

    `pool` selects the candidate-bus set via `rescale_genx_demand.pool_src`:
    "cats_loaded" (the deliverable default) restricts it to buses CATS itself
    loads, plus any bus the chosen map reaches by a direct name match. "all" is
    the GenX pipeline's wider 2026-08-12 pool.

    `uncovered` selects how a county's UNCOVERED pool is split among its uncovered
    buses, via `rescale_genx_demand.uncovered_src`: "cats" (the deliverable
    default) in proportion to each bus's own load in CATS's demand table, "equal"
    flat.  The pool SIZE and every county total are identical either way; this is
    the largest axis in the package because the uncovered pool carries ~51% of
    CA-wide load.
    """
    args = Namespace(map=map_name, system=system, alpha="ratio", county_year=county_year,
                     county_weights=weight_src, weights="reedsco", pool=pool,
                     uncovered=uncovered, external_loads=external,
                     bus_types=bus_types,
                     draw=draw, min_draws=MIN_DRAWS, stochastic_run=run,
                     year=stoch_weight_year(run) if weight_src == "stoch" else None)
    # note: args.year here is the Approach-2 WEIGHT basis year, unrelated to the
    # target year -- see stoch_weight_year
    shares, county_detail, meta = RS.county_first_shares(args, cache)
    per_cell, meta2 = RS.expand_shares_to_cells(shares, county_detail, args, cache, ALL_CELLS)
    nodes = sorted(per_cell.node.unique(), key=int)
    return nodes, share_matrix(per_cell, nodes), county_detail, {**meta, **meta2}


def stoch_weight_year(run: str) -> int:
    """Latest year present in the Approach 2 run's per-substation output.

    `substation_annual(args)` selects one year of that run as the WEIGHT basis.
    It is a relative quantity -- the shares are renormalized -- so this year has
    nothing to do with 2035 and carries no level into the deliverable. The
    CATS-calibrated run covers only the rep-week year (2019).
    """
    path = (ROOT / "data/processed/load_projection/projections" / run
            / "substation_annual_mwh.csv")
    if not path.exists():
        raise FileNotFoundError(
            f"stochastic run not found: {path}\nRun "
            f"scripts/load_projection/approach2/generate_stochastic.py --save-cells first.")
    return int(pd.read_csv(path, usecols=["year"]).year.max())


def stoch_variant(map_name: str, run: str, cache: dict, system: str = "CATS"
                  ) -> tuple[list[str], np.ndarray, pd.DataFrame, dict]:
    args = Namespace(map=map_name, system=system, weights="stoch", level="monthhour",
                     stoch_gate=0.30, stoch_topoff="equal", draw="mean", min_draws=3,
                     stochastic_run=run, year=stoch_weight_year(run))
    shares, coverage, meta = RS.stoch_pool_shares(args, cache)
    per_cell, meta2 = RS.expand_stoch_shares_to_cells(
        shares, args, cache, ALL_CELLS, meta["beta_equal_pool"])
    # the GenX path keeps zero-share rows so a hold redistribution can zero those
    # buses; here there is no control to hold, so drop them and renormalize
    per_cell = per_cell[per_cell.share > 0].copy()
    tot = per_cell.groupby(["month", "hour_pst"]).share.transform("sum")
    per_cell["share"] = per_cell.share / tot
    nodes = sorted(per_cell.node.unique(), key=int)
    return nodes, share_matrix(per_cell, nodes), coverage, {**meta, **meta2}


# ---------------------------------------------------------------------------
# 3. Write one hourly file (chunked: 8,760 x 3,768 is too big to hold 4 copies)
# ---------------------------------------------------------------------------

def write_hourly(path: Path, idx: pd.DataFrame, nodes: list[str], S: np.ndarray,
                 y: np.ndarray, decimals: int, chunk: int = 1000) -> dict:
    """Write the wide CSV.gz; return summary stats taken from the SNAPPED values."""
    cells = idx.cell.to_numpy()
    stamps = idx.datetime_pst.dt.strftime("%Y-%m-%d %H:%M:%S").to_numpy()
    hoy = idx.hour_of_year.to_numpy()
    cols = [f"Demand_MW_z{n}" for n in nodes]
    annual = np.zeros(len(nodes))
    peak = np.full(len(nodes), -np.inf)
    worst_err, n_neg, min_mw, sys_peak = 0.0, 0, np.inf, -np.inf

    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", newline="") as fh:
        fh.write("datetime_pst,hour_of_year," + ",".join(cols) + "\n")
        for a in range(0, len(idx), chunk):
            b = min(a + chunk, len(idx))
            blk = y[a:b, None] * S[cells[a:b], :]
            snapped = round_to_printed(blk, y[a:b], decimals=decimals)
            worst_err = max(worst_err, float(np.abs(snapped.sum(axis=1) - y[a:b]).max()))
            annual += snapped.sum(axis=0)
            peak = np.maximum(peak, snapped.max(axis=0))
            n_neg += int((snapped < 0).sum())
            min_mw = min(min_mw, float(snapped.min()))
            sys_peak = max(sys_peak, float(snapped.sum(axis=1).max()))
            df = pd.DataFrame(snapped, columns=cols)
            df.insert(0, "hour_of_year", hoy[a:b])
            df.insert(0, "datetime_pst", stamps[a:b])
            df.to_csv(fh, index=False, header=False, float_format=f"%.{decimals}f")

    return {"annual": pd.Series(annual, index=nodes), "peak": pd.Series(peak, index=nodes),
            "annual_twh": annual.sum() / 1e6, "peak_mw": sys_peak,
            "n_buses": len(nodes), "n_negative_cells": n_neg, "min_mw": min_mw,
            "max_hourly_conservation_error_mw": worst_err,
            "file_mb": path.stat().st_size / 1e6}


# ---------------------------------------------------------------------------

def _show(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def write_compact(out: Path, series: dict, variants: list, args,
                  meta_cols: dict) -> tuple[dict, dict, list]:
    """Ship each variant's share matrix ONCE plus one statewide series per job.

    The share matrices are invariant across model year and weather year -- they
    depend only on county-year, pool, bus-types, uncovered split, map and weight
    source -- so a grid of combinations needs the allocation stored once. The
    hourly file for any combination is `y[t] * S[cell(t), i]`, which
    `code/expand.py` reconstructs byte-for-byte.

    Per-bus annual energy and peak are still reported exactly, without expanding
    anything: because the share vector is constant within a cell,

        annual_i = sum_c (sum of y over cell c) * S[c, i]
        peak_i   = max_c (max of y over cell c) * S[c, i]

    Both are computed PRE-rounding, so they differ from the expanded files by at
    most half a print unit per hour (0.05 MW at --decimals 1); the expanded file
    is the authority and carries the exact snapped values.
    """
    (out / "shares").mkdir(parents=True, exist_ok=True)
    (out / "statewide").mkdir(parents=True, exist_ok=True)
    n_cells = len(cell_frame_index())

    print(f"\n--- Writing compact package "
          f"({len(variants)} share matrix(es) + {len(series)} series) ---")
    share_mb = 0.0
    for tag, nodes, S in variants:
        p = out / "shares" / f"{tag}.npz"
        np.savez_compressed(p, shares=np.asarray(S, dtype=np.float64),
                            nodes=np.asarray([str(n) for n in nodes]))
        share_mb += p.stat().st_size / 1e6
        print(f"  shares/{tag}.npz  {S.shape[0]} cells x {S.shape[1]:,} buses  "
              f"{p.stat().st_size/1e6:.2f} MB")

    series_mb = 0.0
    for label, (idx_j, y_j) in series.items():
        d = idx_j[["datetime_pst", "hour_of_year", "cell"]].copy()
        d["datetime_pst"] = d.datetime_pst.dt.strftime("%Y-%m-%d %H:%M:%S")
        d["y_mw"] = y_j
        p = out / "statewide" / f"{label}.csv.gz"
        d.to_csv(p, index=False, float_format="%.6f")
        series_mb += p.stat().st_size / 1e6

    annual, peak, rows = {}, {}, []
    for label, (idx_j, y_j) in series.items():
        c = idx_j.cell.to_numpy(dtype=np.int64)
        cell_sum = np.bincount(c, weights=y_j, minlength=n_cells)
        cell_max = np.zeros(n_cells)
        np.maximum.at(cell_max, c, y_j)
        for tag, nodes, S in variants:
            a = S.T @ cell_sum
            pk = (S * cell_max[:, None]).max(axis=0)
            key = f"{tag}__{label}"
            annual[key] = pd.Series(a, index=nodes)
            peak[key] = pd.Series(pk, index=nodes)
            rows.append({"variant": tag, "series": label, **meta_cols,
                         "hours": len(y_j), "n_buses": len(nodes),
                         "annual_twh": a.sum() / 1e6, "peak_mw": float(pk.max()),
                         "n_buses_zero": int((a <= 0).sum()),
                         "summary_basis": "pre-rounding (expand.py snaps to the grid)"})
    print(f"  statewide/: {len(series)} series, {series_mb:.2f} MB")
    print(f"  total {share_mb + series_mb:.1f} MB for "
          f"{len(variants) * len(series)} combination(s) -- expand any of them "
          f"with code/expand.py")
    return annual, peak, rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--year", type=int, default=DEFAULT_TARGET_YEAR,
                    help="RESOLVE model year to project (2024-2045, default 2035)")
    ap.add_argument("--weather-year", type=int, default=None,
                    help="single weather year; --weather-years supersedes it")
    ap.add_argument("--period", default=None,
                    help="what to build. A YEAR (2035) or a comma LIST of years "
                         "(2026,2030,2035) gives independent snapshots, crossed "
                         "with every --weather-years value. A RANGE (2030-2035) "
                         "or a DATE SPAN (2030-01-01:2031-06-30) gives ONE "
                         "continuous series, with weather years paired one per "
                         "calendar year and the last year truncated to the "
                         "period end. Defaults to --year.")
    ap.add_argument("--weather-years", default=None,
                    help="comma list, e.g. 2007,2008,...,2014. Snapshots take the "
                         "cross product; a continuous period needs either one "
                         "(repeated) or exactly one per calendar year, in order.")
    ap.add_argument("--weights", default="envelope,stoch",
                    help="which within-county weight families to ship, from "
                         "{envelope, stoch, external}. 'external' is implied by "
                         "--external-loads. Default envelope,stoch -- the "
                         "published set.")
    ap.add_argument("--no-uncovered-comparison", action="store_true",
                    help="do not write the alternate-uncovered comparison file. "
                         "Per-run only: --uncovered keeps its default and the "
                         "comparison stays on for every other package.")
    ap.add_argument("--format", choices=["hourly", "compact"], default="hourly",
                    help="hourly (default) writes a full wide CSV per variant per "
                         "combination -- ~25-31 MB each. compact writes each "
                         "variant's share matrix ONCE plus one statewide series "
                         "per combination, with code/expand.py to reconstruct "
                         "any hourly file; a 40-combination grid is ~15 MB "
                         "instead of ~4 GB, and the two are identical.")
    ap.add_argument("--county-year", type=int, default=2023)
    ap.add_argument("--load-basis", choices=["net", "gross"], default="net",
                    help="net (DEFAULT) disaggregates load net of BTM PV -- the "
                         "consistent pairing, since substation weights are measured "
                         "downstream of rooftop solar. gross disaggregates RESOLVE's "
                         "gross load, putting the ~43 TWh BTM offset onto the "
                         "substations; use only when the gross series is what is wanted")
    ap.add_argument("--overlays", choices=["all", "none"], default="all",
                    help="all (DEFAULT) = RESOLVE's actual load: Baseline plus every "
                         "overlay component. none = Baseline ONLY, which drops all "
                         "electrification and data-centre growth (CA-wide 2035 gross "
                         "327.35 TWh vs 445.76). Use none only when a caller wants "
                         "that specific series, e.g. to test an algorithm")
    ap.add_argument("--bus-types", choices=["all", "substation"], default="all",
                    help="all (DEFAULT) = Type='Substation' buses plus the AddedNode "
                         "buses CATS loads. substation = physical substations only, "
                         "dropping every AddedNode including the 607 CATS loads; "
                         "their load is redistributed within their own county, so "
                         "county totals are unchanged")
    ap.add_argument("--uncovered", choices=["cats", "equal"], default="cats",
                    help="how each county's uncovered pool is split: cats "
                         "(default) in proportion to the bus's own CATS demand, "
                         "equal flat. An `equalsplit` comparison variant is "
                         "always written alongside, so this sets the PRIMARY")
    ap.add_argument("--pool", choices=["cats_loaded", "all"], default="cats_loaded",
                    help="cats_loaded (default): place load only on buses CATS "
                         "itself loads, plus buses the map reaches by direct name "
                         "match. all: the GenX pipeline's wider pool, which also "
                         "loads Type='Substation' buses CATS leaves at zero")
    ap.add_argument("--external-loads", default="",
                    help="comma-separated folder name(s) under "
                         "data/processed/load_projection/external_loads/, each "
                         "adding a county-first variant whose within-county "
                         "weight comes from that external measurement. Built by "
                         "scripts/load_projection/external_loads/build_external_weights.py")
    ap.add_argument("--maps", default="prox,voltres")
    ap.add_argument("--system", default="CATS",
                    help="nodal artifact namespace: read substation->bus maps "
                         "from data/processed/load_projection/nodal/<system>/. "
                         "Default CATS is the published vintage. A separate "
                         "namespace isolates a run from it; any --external-loads "
                         "artifact MUST have been built with the same --system, "
                         "which the builder checks"),
    ap.add_argument("--draws", default="0,1",
                    help="Approach-2 draw indices shipped alongside the mean; "
                         "empty string ships the mean only")
    ap.add_argument("--stochastic-run",
                    default="stochastic__cats_caiso_target__normal__Fcal__native__calibtgt")
    ap.add_argument("--decimals", type=int, default=1)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--readme", default=str(README_SRC),
                    help="README template to copy into the package; override when a "
                         "non-default run (e.g. --overlays none) needs its own, since "
                         "the default template quotes the default run's numbers")
    ap.add_argument("--no-zip", action="store_true")
    args = ap.parse_args()
    maps = [m.strip() for m in args.maps.split(",") if m.strip()]

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    for sub in ("nodal_hourly", "summary", "reference", "code"):
        (out / sub).mkdir(parents=True)

    print(f"--- RESOLVE {args.year} target, all six CA zones ---")
    if args.overlays == "none":
        print("  !! --overlays none: BASELINE ONLY -- not RESOLVE's full load; all "
              "electrification and data-centre growth is excluded.")
    if args.bus_types == "substation":
        print("  !! --bus-types substation: every AddedNode is unloaded, including "
              "the 607 CATS itself loads.")
    if args.load_basis == "gross":
        print("  !! --load-basis gross: the BTM offset is NOT removed, so it is "
              "allocated onto net-of-meter substation weights.")

    plan, prebuilt = resolve_plan(args)
    for note in plan.notes:
        print(f"  {note}")
    print(f"  {plan.describe()}")

    print("\n--- Allocation (imported from rescale_genx_demand) ---")
    cache: dict = {}
    variants, county_detail = [], None
    prim, alt = args.uncovered, ("equal" if args.uncovered == "cats" else "cats")
    lbl = {"cats": "catsprop", "equal": "equalsplit"}
    fams = {x.strip() for x in args.weights.split(",") if x.strip()}
    bad_fam = sorted(fams - {"envelope", "stoch", "external"})
    if bad_fam:
        raise SystemExit(f"--weights: unknown family {bad_fam}; "
                         f"expected envelope, stoch or external")
    specs = []
    if "envelope" in fams:
        specs += [(f"countyfirst_envelope_{lbl[prim]}__{m}", m, "envelope",
                   "mean", prim, None) for m in maps]
        # the uncovered axis is the largest one, so it gets a comparison file
        if not args.no_uncovered_comparison:
            specs.append((f"countyfirst_envelope_{lbl[alt]}__{maps[0]}",
                          maps[0], "envelope", "mean", alt, None))
    if "stoch" in fams:
        specs.append((f"countyfirst_stochmean_{lbl[prim]}__{maps[0]}",
                      maps[0], "stoch", "mean", prim, None))
        specs += [(f"countyfirst_stochdraw{d}_{lbl[prim]}__{maps[0]}",
                   maps[0], "stoch", d, prim, None)
                  for d in [x.strip() for x in args.draws.split(",") if x.strip()]]
    # each --external-loads folder becomes one more county-first variant; the
    # folder name already records its shape mode and map, so it IS the tag
    specs += [(f"countyfirst_{ext}_{lbl[prim]}__{maps[0]}",
               maps[0], "external", "mean", prim, ext)
              for ext in [x.strip() for x in args.external_loads.split(",") if x.strip()]]
    for tag, m, src, draw, unc, ext in specs:
        nodes, S, detail, meta = county_first_variant(
            m, args.county_year, cache, src, draw, args.stochastic_run, args.pool,
            unc, ext, args.bus_types, system=args.system)
        if county_detail is None:
            county_detail = detail
        print(f"  {tag:<44} {len(nodes):,} buses, "
              f"measured pool {meta['envelope_governed_share']:.3f}, "
              f"covered {int(detail.n_substation_nodes.sum()):,}, "
              f"uncovered split {meta['uncovered_split']}")
        variants.append((tag, nodes, S))
    if not variants:
        raise SystemExit("no variants selected: --weights chose no family and "
                         "--external-loads named no folder")

    # ---- assemble each job's statewide series -----------------------------
    # One build_target per MODEL year (it carries all weather years), so a grid
    # costs one assembly per model year, not one per combination.
    print(f"\n--- Statewide series: {len(plan.jobs)} job(s) ---")
    ref = out / "reference"
    series: dict[str, tuple[pd.DataFrame, np.ndarray]] = {}
    for my in plan.model_years:
        if prebuilt and prebuilt[0] == my:
            _, target, scaling = prebuilt
        else:
            target, scaling = build_target(my, OVERLAY_SCENARIO, args.overlays)
        scaling.to_csv(ref / f"resolve_scaling_y{my}.csv", index=False)
        comps = scaling.attrs.get("components")
        if comps is not None:
            comps.sort_values(["zone", "twh"], ascending=[True, False]).to_csv(
                ref / f"resolve_components_y{my}.csv", index=False)
        for job in [j for j in plan.jobs if j.model_year == my]:
            idx_j, y_j = ca_series(target, job.weather_year, args.load_basis)
            if not job.is_full_year:
                sl = slice(job.hour_start - 1, job.hour_end)
                idx_j, y_j = idx_j.iloc[sl].reset_index(drop=True), y_j[sl]
            series[job.label] = (idx_j, y_j)
            tw = target[target.weather_year == job.weather_year]
            per_zone = tw.groupby("zone")[f"{args.load_basis}_mw"].sum() / 1e6
            iou = per_zone[per_zone.index.isin(IOUS)].sum()
            print(f"  {job.label:<26} {len(idx_j):>6,} h  "
                  f"{y_j.sum()/1e6:7.2f} TWh  peak {y_j.max():>7,.0f} MW  "
                  f"IOU {iou:.1f} + non-IOU {per_zone.sum()-iou:.1f} TWh")
            if job.is_full_year and len(plan.jobs) == 1:
                # the precedent package's own reference file, unchanged
                tw.assign(datetime_pst=tw.datetime_pst.dt.strftime(
                    "%Y-%m-%d %H:%M:%S")).to_csv(
                    ref / "resolve_2035_statewide.csv", index=False,
                    columns=["datetime_pst", "hour_of_year", "zone", "baseline_mw",
                             "overlay_mw", "gross_mw", "btm_mw", "net_mw"],
                    float_format="%.4f")
                scaling.to_csv(ref / "resolve_2035_scaling.csv", index=False)
                if comps is not None:
                    comps.sort_values(["zone", "twh"], ascending=[True, False]).to_csv(
                        ref / "resolve_2035_components.csv", index=False)
        del target

    # a continuous period is ONE series: concatenate its per-year slices in order
    if plan.mode == CONTINUOUS:
        label = f"y{plan.jobs[0].model_year}-{plan.jobs[-1].model_year}"
        idx_all = pd.concat([series[j.label][0] for j in plan.jobs], ignore_index=True)
        y_all = np.concatenate([series[j.label][1] for j in plan.jobs])
        series = {label: (idx_all, y_all)}
        print(f"  concatenated -> {label}: {len(idx_all):,} hours, "
              f"{y_all.sum()/1e6:.2f} TWh")

    # ---- write ------------------------------------------------------------
    meta_cols = {"load_basis": args.load_basis, "overlays": args.overlays,
                 "bus_types": args.bus_types, "uncovered": args.uncovered,
                 "pool": args.pool, "system": args.system}
    if args.format == "compact":
        annual, peak, rows = write_compact(out, series, variants, args, meta_cols)
    else:
        print(f"\n--- Writing hourly files "
              f"({len(variants)} variant(s) x {len(series)} series) ---")
        annual, peak, rows = {}, {}, []
        one = len(series) == 1
        for slabel, (idx_j, y_j) in series.items():
            for tag, nodes, S in variants:
                name = f"{tag}.csv.gz" if one else f"{tag}__{slabel}.csv.gz"
                st = write_hourly(out / "nodal_hourly" / name, idx_j, nodes, S,
                                  y_j, args.decimals)
                key = tag if one else f"{tag}__{slabel}"
                annual[key], peak[key] = st.pop("annual"), st.pop("peak")
                rows.append({"variant": tag, "series": slabel, **meta_cols, **st})
                print(f"  {key:<40} {st['n_buses']:>5} buses  "
                      f"{st['annual_twh']:7.2f} TWh  peak {st['peak_mw']:,.0f} MW  "
                      f"err {st['max_hourly_conservation_error_mw']:.3g} MW  "
                      f"neg {st['n_negative_cells']}  {st['file_mb']:5.1f} MB")

    pd.DataFrame(rows).to_csv(out / "summary/variant_summary.csv", index=False)
    if len(annual) <= 12:
        pd.DataFrame(annual).rename_axis("bus_i").to_csv(
            out / "summary/node_annual_mwh.csv")
        pd.DataFrame(peak).rename_axis("bus_i").to_csv(
            out / "summary/node_peak_mw.csv")
    else:
        # a grid would make a very wide table; long format, gzipped
        for name, d in (("node_annual_mwh", annual), ("node_peak_mw", peak)):
            pd.concat([v.rename("value").rename_axis("bus_i").reset_index()
                       .assign(series_variant=k) for k, v in d.items()],
                      ignore_index=True)[["series_variant", "bus_i", "value"]].to_csv(
                out / f"summary/{name}.csv.gz", index=False)
    county_detail.sort_values("county_share", ascending=False).to_csv(
        out / "summary/county_allocation.csv", index=False)

    cb = RS.candidate_buses()[["node", "fips_int", "county_name"]]
    buses = pd.read_csv(CATS_BUSES)
    for c in buses.columns:
        if pd.api.types.is_string_dtype(buses[c]):
            buses[c] = buses[c].str.strip().str.strip("'").str.strip()
    buses["node"] = buses.bus_i.astype(str)
    all_nodes = sorted(set().union(*[set(a.index) for a in annual.values()]), key=int)
    (buses[buses.node.isin(all_nodes)].merge(cb, on="node", how="left")
     .drop(columns=["node"]).to_csv(ref / "cats_node_metadata.csv", index=False))

    # the first result actually produced -- a grid need not contain `envelope`,
    # and the keys carry the series label when there is more than one series
    primary = next(iter(annual))
    sh = (annual[primary] / annual[primary].sum()).rename("period_average_share")
    (sh.rename_axis("node").reset_index().merge(cb, on="node", how="left")
     .rename(columns={"node": "bus_i"})
     .to_csv(ref / "node_shares_static.csv", index=False))
    for m in maps:
        shutil.copy(NODAL_DIR / MAP_LABELS[m], ref / f"substation_node_map__{m}.csv")

    shutil.copy(Path(__file__), out / "code" / Path(__file__).name)
    shutil.copy(Path(__file__).with_name("deliverable_numbers.py"), out / "code")
    if args.format == "compact":
        shutil.copy(Path(__file__).with_name("expand_template.py"),
                    out / "code" / "expand.py")
    shutil.copy(Path(args.readme), out / "README.md")

    if not args.no_zip:
        zpath = out.with_suffix(".zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(out.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(out.parent))
        print(f"\nZip: {_show(zpath)}  ({zpath.stat().st_size/1e6:.1f} MB)")
    print(f"Package: {_show(out)}")


if __name__ == "__main__":
    main()
