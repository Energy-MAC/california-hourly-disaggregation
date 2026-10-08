"""Parse a requested period and weather years into an ordered list of build jobs.

The deliverable builder works one (model year, weather year) at a time: RESOLVE's
model year sets the LEVEL and the weather year supplies the 8,760-hour SHAPE. This
module turns a user's request for several of them into the exact list of pairs to
build, and decides whether those pairs are independent snapshots or consecutive
slices of one continuous series.

Grammar
-------

    2035                      one snapshot
    2026,2030,2035            three snapshots
    2030-2035                 ONE continuous series, calendar years 2030..2035
    2030-01-01:2031-06-30     ONE continuous series, 1.5 years

A **comma list** (or a bare year) means snapshots; a **range** or a **date span**
means one continuous series. That split is what makes the weather-year rule
unambiguous:

| mode | weather years | rule |
|---|---|---|
| snapshots | **cross product** | every model year x every weather year |
| continuous | **paired** | one per calendar year, in order; a single value repeats |

For a continuous period covering a fractional number of years the list length must
equal the number of calendar years the period TOUCHES -- 2030-01-01:2031-06-30
touches 2030 and 2031, so two weather years, and the second contributes only its
first six months. "ceil(n_years)" and "calendar years touched" are the same number,
and the latter is what the code actually checks.

Hour indexing
-------------

RESOLVE is an 8,760-hour year, and the shipped `datetime_pst` carries the WEATHER
year's calendar while the model year sets only the level (see the builder's module
docstring). So a sub-year window is expressed as a range of `hour_of_year`
(1-based, inclusive), derived from the requested month/day. A requested Feb 29 has
no counterpart in an 8,760-hour year and is dropped, which `parse_period` says
out loud rather than silently shifting the window.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass

HOURS_PER_YEAR = 8760

SNAPSHOTS = "snapshots"
CONTINUOUS = "continuous"

_YEAR = re.compile(r"^\d{4}$")
_YEAR_RANGE = re.compile(r"^(\d{4})\s*-\s*(\d{4})$")
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


@dataclass(frozen=True)
class Job:
    """One (model year, weather year) build, optionally a window within the year."""

    model_year: int
    weather_year: int
    hour_start: int = 1                 # 1-based, inclusive
    hour_end: int = HOURS_PER_YEAR      # inclusive

    @property
    def n_hours(self) -> int:
        return self.hour_end - self.hour_start + 1

    @property
    def is_full_year(self) -> bool:
        return self.hour_start == 1 and self.hour_end == HOURS_PER_YEAR

    @property
    def label(self) -> str:
        base = f"y{self.model_year}_wy{self.weather_year}"
        return base if self.is_full_year else f"{base}_h{self.hour_start}-{self.hour_end}"


@dataclass(frozen=True)
class Plan:
    """What to build. `mode` decides whether the jobs are one series or many."""

    mode: str
    jobs: tuple[Job, ...]
    notes: tuple[str, ...] = ()

    @property
    def model_years(self) -> tuple[int, ...]:
        return tuple(dict.fromkeys(j.model_year for j in self.jobs))

    @property
    def weather_years(self) -> tuple[int, ...]:
        return tuple(dict.fromkeys(j.weather_year for j in self.jobs))

    @property
    def total_hours(self) -> int:
        return sum(j.n_hours for j in self.jobs)

    def describe(self) -> str:
        if self.mode == SNAPSHOTS:
            return (f"{len(self.jobs)} snapshot(s): "
                    f"{len(self.model_years)} model year(s) x "
                    f"{len(self.weather_years)} weather year(s)")
        return (f"one continuous series of {self.total_hours:,} hours over "
                f"{len(self.jobs)} calendar year(s) "
                f"{self.jobs[0].model_year}-{self.jobs[-1].model_year}")


def _day_of_year(month: int, day: int) -> int:
    """1-based day of a NON-leap year; Feb 29 is not representable."""
    if month == 2 and day == 29:
        raise ValueError("Feb 29")
    return sum(calendar.mdays[1:month]) + day


def _parse_date(tok: str) -> tuple[int, int, int]:
    m = _DATE.match(tok)
    if not m:
        raise ValueError(
            f"{tok!r} is not a date; use YYYY-MM-DD, e.g. 2030-01-01")
    y, mo, d = (int(x) for x in m.groups())
    if not 1 <= mo <= 12:
        raise ValueError(f"{tok!r}: month {mo} is out of range")
    last = calendar.monthrange(y, mo)[1]
    if not 1 <= d <= last:
        raise ValueError(f"{tok!r}: {y}-{mo:02d} has {last} days")
    return y, mo, d


def parse_period(period: str) -> tuple[str, object, list[str]]:
    """Classify the request. Returns (mode, payload, notes).

    payload is a list of model years for SNAPSHOTS, or
    ((y, m, d), (y, m, d)) for CONTINUOUS.
    """
    s = str(period).strip()
    notes: list[str] = []
    if not s:
        raise ValueError("--period is empty")

    if ":" in s:
        parts = [p.strip() for p in s.split(":")]
        if len(parts) != 2:
            raise ValueError(f"{s!r}: a date span is START:END, e.g. "
                             f"2030-01-01:2031-06-30")
        try:
            start = _parse_date(parts[0])
            end = _parse_date(parts[1])
        except ValueError as e:
            if str(e) == "Feb 29":
                raise ValueError(
                    f"{s!r}: RESOLVE is an 8,760-hour year with no Feb 29. "
                    f"Use Feb 28 or Mar 1.") from None
            raise
        if (end[0], end[1], end[2]) < (start[0], start[1], start[2]):
            raise ValueError(f"{s!r}: the end is before the start")
        return CONTINUOUS, (start, end), notes

    m = _YEAR_RANGE.match(s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if b < a:
            raise ValueError(f"{s!r}: the end year is before the start year")
        if b == a:
            notes.append(f"{s!r} is a single year; treating it as one snapshot")
            return SNAPSHOTS, [a], notes
        return CONTINUOUS, ((a, 1, 1), (b, 12, 31)), notes

    toks = [t.strip() for t in s.split(",") if t.strip()]
    if toks and all(_YEAR.match(t) for t in toks):
        years = [int(t) for t in toks]
        dup = sorted({y for y in years if years.count(y) > 1})
        if dup:
            raise ValueError(f"{s!r}: model year(s) {dup} listed more than once")
        return SNAPSHOTS, sorted(years), notes

    raise ValueError(
        f"{s!r} is not a period. Use a year (2035), a comma list of years "
        f"(2026,2030,2035) for snapshots, a year range (2030-2035) or a date span "
        f"(2030-01-01:2031-06-30) for one continuous series.")


def parse_weather_years(spec) -> list[int]:
    """`2012` or `2007,2008,...` -> an ordered list, duplicates rejected."""
    if spec is None:
        return []
    if isinstance(spec, int):
        return [spec]
    toks = [t.strip() for t in str(spec).split(",") if t.strip()]
    if not toks:
        return []
    bad = [t for t in toks if not _YEAR.match(t)]
    if bad:
        raise ValueError(f"--weather-years: {bad} are not four-digit years")
    ys = [int(t) for t in toks]
    dup = sorted({y for y in ys if ys.count(y) > 1})
    if dup:
        raise ValueError(f"--weather-years: {dup} listed more than once")
    return ys


def build_plan(period: str, weather_years, available_model_years,
               available_weather_years) -> Plan:
    """Turn a request into the ordered jobs to build, or refuse with a reason.

    `available_*` are the years the SOURCE actually has. An out-of-range request
    fails here, naming what is available, rather than silently producing a
    projection -- which is the one error the load-profile skill says corrupts a
    deliverable without tripping any other check.
    """
    mode, payload, notes = parse_period(period)
    wys = parse_weather_years(weather_years)
    avail_my = sorted(set(int(y) for y in available_model_years))
    avail_wy = sorted(set(int(y) for y in available_weather_years))

    if not wys:
        raise ValueError(
            f"--weather-years is required; the source has "
            f"{avail_wy[0]}-{avail_wy[-1]} ({len(avail_wy)} years)")
    bad_wy = [y for y in wys if y not in avail_wy]
    if bad_wy:
        raise ValueError(
            f"weather year(s) {bad_wy} are not in the source, which has "
            f"{avail_wy[0]}-{avail_wy[-1]} ({len(avail_wy)} years)")

    if mode == SNAPSHOTS:
        years = list(payload)
        bad = [y for y in years if y not in avail_my]
        if bad:
            raise ValueError(
                f"model year(s) {bad} are not in the source, which has "
                f"{avail_my[0]}-{avail_my[-1]}. A year outside that range would "
                f"have to be projected, which this builder will not do.")
        jobs = tuple(Job(my, wy) for my in years for wy in wys)
        notes.append(f"snapshots: {len(years)} model year(s) x {len(wys)} "
                     f"weather year(s) = {len(jobs)} combination(s), "
                     f"cross product")
        return Plan(SNAPSHOTS, jobs, tuple(notes))

    (sy, sm, sd), (ey, em, ed) = payload
    cal_years = list(range(sy, ey + 1))
    bad = [y for y in cal_years if y not in avail_my]
    if bad:
        raise ValueError(
            f"model year(s) {bad} in the requested period are not in the source, "
            f"which has {avail_my[0]}-{avail_my[-1]}. A year outside that range "
            f"would have to be projected, which this builder will not do.")

    if len(wys) == 1:
        paired = wys * len(cal_years)
        if len(cal_years) > 1:
            notes.append(f"one weather year given; repeating {wys[0]} across all "
                         f"{len(cal_years)} calendar year(s)")
    elif len(wys) == len(cal_years):
        paired = wys
    else:
        raise ValueError(
            f"a continuous period over {len(cal_years)} calendar year(s) "
            f"({sy}-{ey}) needs either 1 weather year (repeated) or exactly "
            f"{len(cal_years)}, one per year, in order; got {len(wys)}")

    jobs = []
    for i, (my, wy) in enumerate(zip(cal_years, paired)):
        first = (sm, sd) if i == 0 else (1, 1)
        last = (em, ed) if i == len(cal_years) - 1 else (12, 31)
        try:
            h0 = (_day_of_year(*first) - 1) * 24 + 1
            h1 = _day_of_year(*last) * 24
        except ValueError:
            raise ValueError(
                f"the period touches Feb 29, which an 8,760-hour year does not "
                f"have; use Feb 28 or Mar 1") from None
        jobs.append(Job(my, wy, h0, h1))

    truncated = [j for j in jobs if not j.is_full_year]
    if truncated:
        notes.append(
            "partial calendar year(s): " + ", ".join(
                f"{j.model_year} hours {j.hour_start}-{j.hour_end} "
                f"({j.n_hours:,} of {HOURS_PER_YEAR:,})" for j in truncated))
    return Plan(CONTINUOUS, tuple(jobs), tuple(notes))
