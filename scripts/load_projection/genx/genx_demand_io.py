"""Byte-safe read/write of GenX `system/Demand_data.csv`, plus scenario-tree
helpers shared by rescale_genx_demand.py and materialize_genx_cases.py.

A GenX demand file is a single header row followed by one row per timestep of
the representative period.  The first 9 columns are GenX metadata
(`Time_Index`, `Voll`, `Demand_Segment`, ...) and are *ragged* -- only the
first few rows carry values, the rest are blank -- while the remaining columns
are `Demand_MW_z{zone}`, one per network zone.  For the CATS-based scenarios in
`genx/`, zone `z{i}` is CATS `bus_i`, so the zone id is the nodal-mapping node
id as a string.

Rescaling must not disturb anything except the zone values, so the metadata
columns are carried through as verbatim strings and never parsed.  Zone values
round-trip through `%.1f`, matching the control files' formatting; a read/write
of an untouched control is byte-identical (asserted by the tests).

Functions
---------
  read_demand(path)                 -> GenXDemand (metadata strings + float matrix)
  write_demand(path, demand, values) write with the original column order/format
  round_to_printed(values, targets)  largest-remainder rounding to 0.1 MW that
                                     makes each row's printed sum hit its target
  scenario_seasons(genx_root)        -> ScenarioTree (case -> season, canonical
                                     case per season, verified 4 distinct files)
  load_rep_week_calendar(path)       -> {season: [(month, hour_pst)] * n_hours}

See docs/genx_rescale.md for the methodology this supports.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
GENX_ROOT = ROOT / "genx"
SCENARIO_INDEX = GENX_ROOT / "scenarios_2019.csv"
SCENARIO_TREE = GENX_ROOT / "scenarios_historical_2019/scenarios_historical_2019"
REP_WEEK_CALENDAR = GENX_ROOT / "rep_week_calendar.csv"

ZONE_PREFIX = "Demand_MW_z"
DEMAND_RELPATH = Path("system/Demand_data.csv")


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


@dataclass
class GenXDemand:
    """A parsed GenX demand file.

    columns : original header order, used verbatim on write.
    meta    : the non-zone columns as strings (blanks preserved as "").
    zones   : zone ids in column order, e.g. "1" for `Demand_MW_z1`.
    values  : (n_hours, n_zones) float64, aligned to `zones`.
    """

    path: Path
    columns: list[str]
    meta: pd.DataFrame
    zones: list[str]
    values: np.ndarray

    @property
    def n_hours(self) -> int:
        return self.values.shape[0]

    def hourly_totals(self) -> np.ndarray:
        return self.values.sum(axis=1)

    def zone_index(self) -> dict[str, int]:
        return {z: i for i, z in enumerate(self.zones)}


def read_demand(path: Path) -> GenXDemand:
    """Parse a GenX demand file without touching its metadata columns.

    Every cell is read as a string so ragged blanks and integer-formatted
    metadata (`Voll=200000`) survive a round trip; only the zone columns are
    converted to float.
    """
    path = Path(path)
    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    columns = list(raw.columns)
    zone_cols = [c for c in columns if c.startswith(ZONE_PREFIX)]
    if not zone_cols:
        raise ValueError(f"{path} has no {ZONE_PREFIX}* columns")
    meta_cols = [c for c in columns if not c.startswith(ZONE_PREFIX)]

    values = raw[zone_cols].to_numpy(dtype=np.float64)
    if (values < 0).any():
        n_neg = int((values < 0).sum())
        raise ValueError(
            f"{path} has {n_neg} negative demand values; the rescaler assumes "
            "non-negative control load (redistribution would erase sign)")

    return GenXDemand(
        path=path,
        columns=columns,
        meta=raw[meta_cols],
        zones=[c[len(ZONE_PREFIX):] for c in zone_cols],
        values=values,
    )


def write_demand(path: Path, demand: GenXDemand, values: np.ndarray | None = None,
                 float_format: str = "one_decimal") -> None:
    """Write `values` (default: `demand.values`) back in the source's layout.

    float_format:
      one_decimal -- "%.1f", matching the control files (default)
      full        -- shortest round-trippable repr, for float-precision output
    """
    vals = demand.values if values is None else values
    if vals.shape != demand.values.shape:
        raise ValueError(f"values shape {vals.shape} != source {demand.values.shape}")

    if float_format == "one_decimal":
        formatted = np.char.mod("%.1f", vals)
    elif float_format == "full":
        formatted = np.vectorize(repr)(vals)
    else:
        raise ValueError(f"unknown float_format {float_format!r}")

    zone_df = pd.DataFrame(formatted, columns=[ZONE_PREFIX + z for z in demand.zones])
    out = pd.concat([demand.meta.reset_index(drop=True), zone_df], axis=1)
    out = out[demand.columns]  # restore the source's exact column order
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False, lineterminator="\n")


def round_to_printed(values: np.ndarray, targets: np.ndarray,
                     decimals: int = 1) -> np.ndarray:
    """Round each row to `decimals` places so its sum hits `targets` exactly.

    Naive per-cell rounding leaves a residual of up to n_zones/2 * 10^-decimals
    per row, which would break hourly conservation at the precision the file is
    actually written in.  Largest-remainder apportionment fixes this: work in
    integer units of 10^-decimals, floor everything, then hand the leftover
    units to the cells with the largest discarded fractions (and take units back
    from the smallest when the floor sum overshoots).

    `targets` are the desired row sums in MW; they are themselves snapped to the
    unit grid first, since a target off-grid is unreachable by construction.
    """
    scale = 10 ** decimals
    scaled = values * scale
    floor = np.floor(scaled)
    remainder = scaled - floor
    target_units = np.rint(np.asarray(targets, dtype=np.float64) * scale)

    out = floor.copy()
    for r in range(out.shape[0]):
        deficit = int(round(target_units[r] - out[r].sum()))
        if deficit == 0:
            continue
        if deficit > 0:  # hand out units to the largest discarded fractions
            order = np.argsort(-remainder[r], kind="stable")[:deficit]
            out[r, order] += 1
        else:  # overshoot: reclaim units, never below zero
            order = np.argsort(remainder[r], kind="stable")
            reclaim = -deficit
            for j in order:
                if reclaim == 0:
                    break
                if out[r, j] >= 1:
                    out[r, j] -= 1
                    reclaim -= 1
            if reclaim:
                raise ValueError(f"row {r}: could not reclaim {reclaim} units")
    return out / scale


@dataclass
class ScenarioTree:
    """The genx/ control scenario tree, grouped by season."""

    root: Path
    case_season: dict[str, str]          # "p1" -> "Summer"
    season_cases: dict[str, list[str]]   # "Summer" -> ["p1", ..., "p7"]
    canonical: dict[str, str]            # "Summer" -> "p1" (the case we read)
    season_md5: dict[str, str]

    def demand_path(self, case: str) -> Path:
        return self.root / case / DEMAND_RELPATH

    def case_dir(self, case: str) -> Path:
        return self.root / case

    @property
    def seasons(self) -> list[str]:
        return list(self.season_cases)

    @property
    def cases(self) -> list[str]:
        return list(self.case_season)


def scenario_seasons(index_path: Path = SCENARIO_INDEX,
                     tree_root: Path = SCENARIO_TREE) -> ScenarioTree:
    """Group the scenario cases by season and verify the demand files agree.

    The scenario grid sweeps renewable weather years within each season, so all
    cases of a season must share one demand file.  That is checked by md5 rather
    than assumed: reading one case per season is only valid if the files are in
    fact identical, and a silent mismatch would mean rescaling the wrong week.
    """
    idx = pd.read_csv(index_path)
    case_season = {f"p{int(n)}": str(s) for n, s in zip(idx["Number"], idx["Temporal"])}

    season_cases: dict[str, list[str]] = {}
    for case, season in case_season.items():
        season_cases.setdefault(season, []).append(case)
    for season in season_cases:
        season_cases[season].sort(key=lambda c: int(c[1:]))

    canonical, season_md5 = {}, {}
    for season, cases in season_cases.items():
        digests = {}
        for case in cases:
            path = tree_root / case / DEMAND_RELPATH
            if not path.exists():
                raise FileNotFoundError(f"missing demand file for {case}: {path}")
            digests[case] = md5(path)
        distinct = set(digests.values())
        if len(distinct) != 1:
            raise ValueError(
                f"season {season!r} has {len(distinct)} distinct demand files "
                f"({digests}); one demand file per season is assumed")
        canonical[season] = cases[0]
        season_md5[season] = digests[cases[0]]

    if len(set(season_md5.values())) != len(season_md5):
        raise ValueError(f"two seasons share a demand file: {season_md5}")

    return ScenarioTree(root=tree_root, case_season=case_season,
                        season_cases=season_cases, canonical=canonical,
                        season_md5=season_md5)


def load_rep_week_calendar(path: Path = REP_WEEK_CALENDAR,
                           n_hours: int = 168) -> dict[str, list[tuple[int, int]]]:
    """{season: [(month, hour_pst)] * n_hours} from the rep-week calendar file.

    Schema: `season,start_datetime,timezone,notes`, where `start_datetime` is
    the wall-clock time of `Time_Index == 1` and hours advance by one from
    there.  Timestamps are converted to the repo's canonical fixed PST
    (UTC-8, hour-beginning, no DST) so month-hour weights line up with
    `hour_pst` in the substation profiles.

    Raises FileNotFoundError if the calendar has not been supplied yet -- see
    docs/genx_rescale.md; `--level static` does not need it.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"rep-week calendar not found at {path}.\n"
            "Month-hour weights need the calendar period of each season's "
            "representative week.  Create it with columns "
            "season,start_datetime,timezone,notes -- one row per season, where "
            "start_datetime is the wall-clock time of Time_Index == 1 "
            '(e.g. "Summer,2019-07-08 00:00,US/Pacific,"). '
            "Until then use --level static.")

    cal = pd.read_csv(path)
    out: dict[str, list[tuple[int, int]]] = {}
    for _, row in cal.iterrows():
        start = pd.Timestamp(row["start_datetime"])
        tz = str(row["timezone"]).strip()
        if tz and tz.lower() not in ("", "nan", "pst", "fixed pst"):
            start = start.tz_localize(tz) if start.tz is None else start.tz_convert(tz)
            start = start.tz_convert("Etc/GMT+8").tz_localize(None)  # fixed PST
        stamps = start + pd.to_timedelta(np.arange(n_hours), unit="h")
        out[str(row["season"])] = list(zip(stamps.month.tolist(), stamps.hour.tolist()))
    return out
