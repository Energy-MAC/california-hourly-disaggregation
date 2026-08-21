"""
LEGACY (retired 2026-08-14) -- supports the out-of-sample study only.

The RESOLVE target exists to check how the fitted model behaves on a series
outside its training data, which is a question about PREDICTION. Approach 2
disaggregates a load series that is already known, so out-of-sample behaviour is
not a property the method needs. Kept because the run is cheap and the finding
(a closed-form, F-invariant level bias) is documented; see
docs/approach2_stochastic.md -> "LEGACY". The script itself is still correct and
still runs -- disaggregating a RESOLVE series is a legitimate thing to do, it is
only the out-of-sample *scoring* that was retired.

--------------------------------------------------------------------------

Build a CAISO-consistent RESOLVE hourly target CSV for generate_stochastic.py's
--target flag (dt_pst_hb, demand_mw columns).

PGE+SCE+SDGE net-of-BTM hourly sum across RESOLVE's 23 weather years
(2000-2022, at the 2024 annual-energy basis) -- the same series
plot_stochastic.py's clt-resolve demo uses, persisted here so it can be fed
to generate_stochastic.py as an OUT-OF-SAMPLE target: RESOLVE was never used
to fit the model's envelope, rho(c), or shape s(c) parameters (those come
from the utility scrape + EIA-930 CAISO 2015-2025), so running the model
against it is not a validation of the model -- it's a look at how the
already-fit model behaves on a target series outside its training data.

Output
------
  data/processed/resolve/resolve_caiso_target.csv
    dt_pst_hb, demand_mw

Usage
-----
  python scripts/load_projection/approach2/build_resolve_target.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from plot_stochastic import load_resolve_target  # noqa: E402

OUT_FILE = ROOT / "data" / "processed" / "resolve" / "resolve_caiso_target.csv"


def main() -> None:
    y = load_resolve_target()
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    y[["dt_pst_hb", "demand_mw"]].to_csv(OUT_FILE, index=False)
    print(f"Wrote {len(y):,} hours ({y.dt_pst_hb.dt.year.min()}-{y.dt_pst_hb.dt.year.max()}) "
          f"-> {OUT_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
