"""Guards for the period / weather-year grammar (src/load_projection/periods.py).

Pure and fast: no data files, no builds. Prints one [OK] per guard and exits
non-zero on the first failure, matching test_genx_rescale.py.

Usage
  python scripts/load_projection/deliverables/test_periods.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from load_projection.periods import (  # noqa: E402
    CONTINUOUS,
    HOURS_PER_YEAR,
    SNAPSHOTS,
    build_plan,
    parse_period,
    parse_weather_years,
)

MY = list(range(2024, 2046))          # RESOLVE model years
WY = list(range(2000, 2023))          # Baseline weather years


def plan(period, wys):
    return build_plan(period, wys, MY, WY)


def refuses(period, wys, fragment, label):
    try:
        plan(period, wys)
    except ValueError as e:
        assert fragment in str(e), f"{label}: expected {fragment!r} in {e}"
    else:
        raise AssertionError(f"{label}: should have refused")


def guard_classification() -> None:
    """A list is snapshots; a range or a date span is one continuous series."""
    assert parse_period("2035")[0] == SNAPSHOTS
    assert parse_period("2026,2030,2035")[0] == SNAPSHOTS
    assert parse_period("2030-2035")[0] == CONTINUOUS
    assert parse_period("2030-01-01:2031-06-30")[0] == CONTINUOUS
    # a degenerate range is a snapshot, and says so
    mode, payload, notes = parse_period("2035-2035")
    assert mode == SNAPSHOTS and payload == [2035] and notes
    print("  [OK] list -> snapshots; range and date span -> continuous")


def guard_snapshot_cross_product() -> None:
    """Snapshots take the cross product: the user's 5 x 8 = 40."""
    p = plan("2026,2030,2035,2040,2045", "2007,2008,2009,2010,2011,2012,2013,2014")
    assert p.mode == SNAPSHOTS
    assert len(p.jobs) == 40, len(p.jobs)
    assert p.model_years == (2026, 2030, 2035, 2040, 2045)
    assert len(p.weather_years) == 8
    assert all(j.is_full_year for j in p.jobs)
    assert p.total_hours == 40 * HOURS_PER_YEAR
    # every model year appears with every weather year, exactly once
    pairs = {(j.model_year, j.weather_year) for j in p.jobs}
    assert len(pairs) == 40
    assert p.jobs[0].label == "y2026_wy2007"
    print(f"  [OK] snapshots cross product: 5 x 8 = {len(p.jobs)} jobs, "
          f"{p.total_hours:,} hours, labels like {p.jobs[0].label!r}")


def guard_continuous_paired() -> None:
    """A range pairs one weather year per calendar year, in order."""
    p = plan("2030-2035", "2007,2008,2009,2010,2011,2012")
    assert p.mode == CONTINUOUS
    assert len(p.jobs) == 6
    assert [j.model_year for j in p.jobs] == [2030, 2031, 2032, 2033, 2034, 2035]
    assert [j.weather_year for j in p.jobs] == [2007, 2008, 2009, 2010, 2011, 2012]
    assert all(j.is_full_year for j in p.jobs)
    assert p.total_hours == 6 * HOURS_PER_YEAR
    # a single weather year repeats
    p1 = plan("2030-2035", "2012")
    assert [j.weather_year for j in p1.jobs] == [2012] * 6
    assert any("repeating" in n for n in p1.notes)
    print(f"  [OK] continuous pairing: 6 calendar years, "
          f"{p.total_hours:,} hours; a single weather year repeats")


def guard_fractional_truncation() -> None:
    """1.5 years needs 2 weather years and truncates the second."""
    p = plan("2030-01-01:2031-06-30", "2012,2013")
    assert p.mode == CONTINUOUS
    assert len(p.jobs) == 2
    a, b = p.jobs
    assert a.is_full_year, "the first full calendar year must not be truncated"
    assert a.weather_year == 2012 and b.weather_year == 2013
    assert b.hour_start == 1
    # Jan 1 - Jun 30 is 181 days of a non-leap year
    assert b.hour_end == 181 * 24, b.hour_end
    assert not b.is_full_year
    assert p.total_hours == HOURS_PER_YEAR + 181 * 24
    assert any("partial calendar year" in n for n in p.notes)
    assert b.label == f"y2031_wy2013_h1-{181 * 24}"
    print(f"  [OK] 1.5 years: 2 weather years, second truncated to "
          f"{b.n_hours:,} of {HOURS_PER_YEAR:,} hours")


def guard_sub_year() -> None:
    """A sub-year window is one calendar year, so exactly one weather year."""
    p = plan("2030-01-01:2030-02-28", "2012")
    assert len(p.jobs) == 1
    j = p.jobs[0]
    assert j.hour_start == 1 and j.hour_end == 59 * 24, (j.hour_start, j.hour_end)
    assert j.n_hours == 59 * 24
    # a mid-year window starts where it should: Jul 1 is day 182
    p2 = plan("2030-07-01:2030-07-31", "2012")
    k = p2.jobs[0]
    assert k.hour_start == 181 * 24 + 1, k.hour_start
    assert k.hour_end == 212 * 24, k.hour_end
    assert k.n_hours == 31 * 24
    # two weather years for a one-calendar-year window is refused
    refuses("2030-01-01:2030-02-28", "2012,2013",
            "needs either 1 weather year", "sub-year with 2 weather years")
    print(f"  [OK] sub-year windows map to hour_of_year "
          f"(Jan1-Feb28 -> 1-{59 * 24}, Jul -> {181 * 24 + 1}-{212 * 24}); "
          f"a second weather year is refused")


def guard_refusals() -> None:
    """Every bad request fails up front, naming what is available."""
    refuses("2030-2035", "2007,2008", "exactly 6", "wrong weather-year count")
    refuses("2050", "2012", "not in the source", "model year out of range")
    refuses("2026,2050", "2012", "not in the source", "one bad year in a list")
    refuses("2044-2046", "2012", "not in the source", "range running past the source")
    refuses("2035", "1999", "not in the source", "weather year out of range")
    refuses("2035", None, "required", "no weather years")
    refuses("2030-02-28:2030-02-01", "2012", "end is before the start", "reversed span")
    refuses("2035-2030", "2012", "before the start year", "reversed range")
    refuses("2026,2026", "2012", "more than once", "duplicate model year")
    refuses("2035", "2012,2012", "more than once", "duplicate weather year")
    refuses("not-a-period", "2012", "is not a period", "garbage")
    # 2032 IS a leap year, so the date is valid in the real calendar but has no
    # slot in an 8,760-hour year; 2031-02-29 is simply not a date at all
    refuses("2032-01-01:2032-02-29", "2012", "8,760-hour", "real leap day")
    refuses("2030-01-01:2031-02-29", "2012,2013", "28 days", "non-leap Feb 29")
    for bad in ("2030-13-01:2030-12-31", "2030-01-32:2030-12-31"):
        refuses(bad, "2012", "out of range" if "13" in bad else "days", bad)
    print("  [OK] 14 bad requests each refused with an actionable message")


def guard_weather_year_parsing() -> None:
    """The weather-year spec accepts an int or a list and rejects junk."""
    assert parse_weather_years(2012) == [2012]
    assert parse_weather_years("2012") == [2012]
    assert parse_weather_years("2007,2008, 2009") == [2007, 2008, 2009]
    assert parse_weather_years(None) == []
    assert parse_weather_years("") == []
    for bad in ("12", "20x2", "2012.5"):
        try:
            parse_weather_years(bad)
        except ValueError as e:
            assert "four-digit" in str(e)
        else:
            raise AssertionError(f"{bad!r} should have been refused")
    print("  [OK] weather-year spec: int, string, list; junk refused")


def main() -> None:
    print("Period / weather-year grammar guards")
    guard_classification()
    guard_snapshot_cross_product()
    guard_continuous_paired()
    guard_fractional_truncation()
    guard_sub_year()
    guard_refusals()
    guard_weather_year_parsing()
    print("all guards passed")


if __name__ == "__main__":
    main()
