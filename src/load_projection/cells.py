"""Cell definitions for the stochastic disaggregation model (Approach 2).

A "cell" is the conditioning unit of the model: every parameter (mu, sigma, s(c),
rho(c)) is estimated per cell, and the target series is standardized within its
own cell. Historically the cell was fixed at (month, hour_pst) -- 288 of them,
encoded as the flat integer `(month - 1) * 24 + hour_pst`. This module makes the
definition pluggable so the same model can run on inputs whose native resolution
is coarser (one load per season) or finer (month-day-hour), without touching any
model equation.

Every spec is represented uniformly by a lookup table `codes` over the canonical
(month, day, hour) slot space of a leap year, so encoding is one fancy-index and
coarsening one spec into another is a table composition. `MONTHHOUR` reproduces
the original arithmetic exactly, including int64 dtype -- guarded by
scripts/load_projection/approach2/test_envelope_cells.py.

Built-in specs (by `name`):

  monthhour     288   (month, hour_pst)          the default; unchanged behavior
  monthdayhour  8784  (month, day, hour_pst)     leap-year calendar, Feb 29 included
  month         12    (month,)
  season3       4     (season,)                  DJF / MAM / JJA / SON
  halfyear      2     (halfyear,)                NovApr / MayOct
  custom:<csv>  n     (cell_label,)              month[,day][,hour_pst] -> cell_label

The halfyear split is `MayOct` = months 5-10, `NovApr` = months 11-4, i.e. spring
is lumped with winter. That is the best of the six contiguous 6-month partitions
of the California load surface by a wide margin (28.9% of month-hour variance
explained vs 3.2% for spring+summer, Mar-Aug): March and April are the year's
two lowest-load months (index 0.880 / 0.881) while August and July are the
highest (1.220 / 1.195) and September is still 1.125, so any split that moves
September into the cool block discards most of the signal. See
docs/stochastic_model_spec.md and scripts/load_projection/approach2/doc_numbers.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

N_MONTHS, N_DAYS, N_HOURS = 12, 31, 24
N_SLOTS = N_MONTHS * N_DAYS * N_HOURS  # canonical (month, day, hour) slot space

SEASON3_LABELS = ("DJF", "MAM", "JJA", "SON")
HALFYEAR_LABELS = ("NovApr", "MayOct")
MAYOCT_MONTHS = (5, 6, 7, 8, 9, 10)

BUILTIN_NAMES = ("monthhour", "monthdayhour", "month", "season3", "halfyear")


def _slot(month, day, hour) -> np.ndarray:
    """Flat index into the canonical (month, day, hour) slot space."""
    m = np.asarray(month, dtype=np.int64)
    d = np.asarray(day, dtype=np.int64)
    h = np.asarray(hour, dtype=np.int64)
    return ((m - 1) * N_DAYS + (d - 1)) * N_HOURS + h


def _valid_slots() -> pd.DataFrame:
    """The 8,784 real (month, day, hour_pst) slots of a leap year, plus derived
    season / halfyear labels. A leap year is used so Feb 29 is a valid cell."""
    days = pd.date_range("2020-01-01", "2020-12-31", freq="D")
    cal = pd.DataFrame({"month": days.month.astype("int64"),
                        "day": days.day.astype("int64")})
    cal = cal.loc[cal.index.repeat(N_HOURS)].reset_index(drop=True)
    cal["hour_pst"] = np.tile(np.arange(N_HOURS, dtype=np.int64), len(days))
    cal["slot"] = _slot(cal.month, cal.day, cal.hour_pst)
    cal["season"] = pd.Categorical(
        [SEASON3_LABELS[(m % 12) // 3] for m in cal.month], categories=SEASON3_LABELS)
    cal["halfyear"] = pd.Categorical(
        np.where(cal.month.isin(MAYOCT_MONTHS), "MayOct", "NovApr"),
        categories=HALFYEAR_LABELS)
    return cal


@dataclass(frozen=True, eq=False)
class CellSpec:
    """An immutable cell definition.

    `codes` maps every canonical slot to a cell index in 0..n_cells-1, with -1
    for slots that are not real calendar hours (e.g. Feb 30) or that a custom
    spec leaves uncovered. `labels` carries the natural CSV columns, one row per
    cell, in cell order.
    """

    name: str
    key_cols: tuple[str, ...]
    needs_day: bool
    needs_hour: bool
    codes: np.ndarray
    n_cells: int
    labels: pd.DataFrame

    def __repr__(self) -> str:  # keep reprs short in run logs
        return f"CellSpec({self.name!r}, n_cells={self.n_cells}, keys={self.key_cols})"


def _make_spec(name: str, keys: pd.DataFrame, cal: pd.DataFrame,
               needs_day: bool, needs_hour: bool,
               order: list[str] | None = None) -> CellSpec:
    """Assemble a spec from a per-valid-slot key frame.

    Cell order is the stable lexicographic sort of the distinct key rows, which
    for (month, hour_pst) is exactly `(month - 1) * 24 + hour_pst`. Categorical
    key columns sort by their declared category order, not alphabetically, so
    `season3` comes out DJF/MAM/JJA/SON rather than DJF/JJA/MAM/SON.
    """
    key_cols = tuple(keys.columns)
    uniq = keys.drop_duplicates().sort_values(
        order or list(key_cols), kind="stable").reset_index(drop=True)
    uniq["cell"] = np.arange(len(uniq), dtype=np.int64)
    merged = keys.merge(uniq, on=list(key_cols), how="left")

    codes = np.full(N_SLOTS, -1, dtype=np.int64)
    codes[cal.slot.to_numpy()] = merged.cell.to_numpy()

    labels = uniq.set_index("cell")
    return CellSpec(name=name, key_cols=key_cols, needs_day=needs_day,
                    needs_hour=needs_hour, codes=codes, n_cells=len(uniq),
                    labels=labels)


def _load_custom(path: Path) -> CellSpec:
    """Read a user-authored `month[,day][,hour_pst],cell_label` mapping.

    Cells are numbered by order of first appearance in the file, so the author
    controls the ordering. Slots the file does not mention stay -1 and raise on
    encode -- a partial calendar is a mistake worth surfacing, not filling in.
    """
    df = pd.read_csv(path)
    if "cell_label" not in df.columns or "month" not in df.columns:
        raise ValueError(f"{path}: custom cell map needs columns 'month' and "
                         f"'cell_label'; got {list(df.columns)}")
    needs_day = "day" in df.columns
    needs_hour = "hour_pst" in df.columns

    cal = _valid_slots()
    on = ["month"] + (["day"] if needs_day else []) + (["hour_pst"] if needs_hour else [])
    order = list(dict.fromkeys(df.cell_label.astype(str)))
    df = df[on + ["cell_label"]].copy()
    df["cell_label"] = pd.Categorical(df.cell_label.astype(str), categories=order)

    keys = cal[on].merge(df, on=on, how="left")[["cell_label"]]
    if keys.cell_label.isna().any():
        n = int(keys.cell_label.isna().sum())
        raise ValueError(f"{path}: custom cell map leaves {n} of {len(keys)} "
                         f"calendar hours unassigned; every hour needs a cell_label")
    return _make_spec(f"custom:{path}", keys, cal, needs_day, needs_hour)


def get_spec(name: str) -> CellSpec:
    """Resolve a spec name. `custom:<path>` loads a user-authored mapping."""
    if isinstance(name, CellSpec):
        return name
    if name.startswith("custom:"):
        return _load_custom(Path(name.split(":", 1)[1]))
    cal = _valid_slots()
    if name == "monthhour":
        return _make_spec(name, cal[["month", "hour_pst"]], cal, False, True)
    if name == "monthdayhour":
        return _make_spec(name, cal[["month", "day", "hour_pst"]], cal, True, True)
    if name == "month":
        return _make_spec(name, cal[["month"]], cal, False, False)
    if name == "season3":
        return _make_spec(name, cal[["season"]], cal, False, False)
    if name == "halfyear":
        return _make_spec(name, cal[["halfyear"]], cal, False, False)
    raise ValueError(f"unknown cell spec {name!r}; expected one of "
                     f"{BUILTIN_NAMES} or 'custom:<path>'")


MONTHHOUR = get_spec("monthhour")


def encode(spec: CellSpec, month, hour=None, day=None) -> np.ndarray:
    """Map calendar fields to cell indices 0..n_cells-1.

    `hour`/`day` may be omitted for specs that do not need them. Raises if a
    required field is missing or if any row lands on a slot the spec does not
    cover.
    """
    m = np.asarray(month, dtype=np.int64)
    if spec.needs_hour and hour is None:
        raise ValueError(f"cell spec {spec.name!r} needs an hour_pst column")
    if spec.needs_day and day is None:
        raise ValueError(f"cell spec {spec.name!r} needs a day column")
    h = np.zeros_like(m) if hour is None else np.asarray(hour, dtype=np.int64)
    d = np.ones_like(m) if day is None else np.asarray(day, dtype=np.int64)
    out = spec.codes[_slot(m, d, h)]
    if (out < 0).any():
        bad = np.flatnonzero(out < 0)[:5]
        raise ValueError(f"cell spec {spec.name!r} does not cover "
                         f"{int((out < 0).sum())} rows, e.g. month={m[bad]} "
                         f"day={d[bad]} hour={h[bad]}")
    return out


def label_frame(spec: CellSpec) -> pd.DataFrame:
    """One row per cell with the spec's natural CSV columns, indexed by `cell`.

    This is what replaces the open-coded `cell // 24 + 1` / `cell % 24` decode.
    """
    return spec.labels.copy()


def coarsen_map(fine: CellSpec, coarse: CellSpec) -> np.ndarray:
    """`[fine.n_cells]` array giving each fine cell's containing coarse cell.

    Valid only when the coarse spec is a genuine aggregation of the fine one:
    every fine cell must fall entirely inside one coarse cell, and the coarse
    spec cannot need a calendar field the fine spec does not resolve.
    """
    if coarse.needs_day and not fine.needs_day:
        raise ValueError(f"cannot coarsen {fine.name!r} to {coarse.name!r}: "
                         f"the target needs a day axis the source does not resolve")
    if coarse.needs_hour and not fine.needs_hour:
        raise ValueError(f"cannot coarsen {fine.name!r} to {coarse.name!r}: "
                         f"the target needs an hour axis the source does not resolve")
    live = fine.codes >= 0
    f = fine.codes[live]
    c = coarse.codes[live]
    out = np.full(fine.n_cells, -1, dtype=np.int64)
    out[f] = c
    # a fine cell straddling two coarse cells would make the aggregation
    # ill-defined; catch it rather than silently keeping the last write
    if not np.array_equal(out[f], c):
        raise ValueError(f"cannot coarsen {fine.name!r} to {coarse.name!r}: "
                         f"some source cells straddle two target cells")
    if (out < 0).any():
        raise ValueError(f"cannot coarsen {fine.name!r} to {coarse.name!r}: "
                         f"{int((out < 0).sum())} source cells have no target cell")
    return out
