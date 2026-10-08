"""Recompute every measured number quoted in the Approach 3 docs.

Per the project rule (user 2026-08-14), no measured figure is hand-edited into a
doc: it comes from here. Sections are labelled with where each number is quoted.
Same pattern as `genx/doc_numbers.py` and `approach2/doc_numbers.py`.

Run:
  python scripts/load_projection/approach3/doc_numbers.py --run <tag>
  python scripts/load_projection/approach3/doc_numbers.py --run <tag> --sections B,C

Sections
  A  target: TWh, peak, block energies, Y(c) dispersion within a block
  B  THE NORMALIZATION NUMBER: exactness under the target-energy-weighted block
     mean (exactly 0) vs the unweighted one
  C  SLACK FEASIBILITY MATRIX: lambda_M / lambda_U / A(b) / min shape_U across
     shape source x shape-common x negative-nodes.  Says which combinations are
     feasible -- this is what the shipped default should be chosen from
  D  negative-level inventory, incl. the net-base vs zero base-total difference
  E  shape-source comparison and the per-sibling decorrelation payoff
  F  rounding: conservation float vs printed, per-node printed drift
  G  mapping coverage at both levels (base and node)

Output is printed and written to data/checks/approach3/doc_numbers.txt.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/genx"))
sys.path.insert(0, str(ROOT / "scripts/load_projection/approach3"))

from load_projection import cells as C  # noqa: E402
from load_projection import proportional as P  # noqa: E402

import build_proportional_nodal as B  # noqa: E402

OUT_FILE = ROOT / "data/checks/approach3/doc_numbers.txt"
RUNS = ROOT / "data/processed/load_projection/approach3"
BLOCKS = C.HALFYEAR_LABELS

_LINES: list[str] = []


def say(*parts) -> None:
    line = " ".join(str(p) for p in parts)
    print(line)
    _LINES.append(line)


def head(title: str, quoted: str) -> None:
    say("")
    say("=" * 78)
    say(title)
    say(f"  quoted in: {quoted}")
    say("=" * 78)


class Ctx:
    """Everything the sections share, rebuilt from the run's own manifest."""

    def __init__(self, tag: str):
        self.dir = RUNS / tag
        if not self.dir.exists():
            raise SystemExit(
                f"{self.dir} not found. Build a run first:\n"
                f"  python scripts/load_projection/approach3/"
                f"build_proportional_nodal.py --nodes ... --mapping ... "
                f"--target-csv ...")
        self.man = json.loads((self.dir / "manifest.json").read_text())
        self.tag = tag
        np_ = Path(self.man["inputs"]["nodes"])
        mp_ = Path(self.man["inputs"]["mapping"])
        self.nodes = P.read_node_table(np_)
        prof = pd.read_csv(B.PROFILES, usecols=["utility", "substation_name"])
        prof["utility"] = prof.utility.str.lower()
        self.edges = P.read_mapping(mp_, self.nodes, prof)
        t = self.man["target"]
        if t["source"] == "csv":
            self.target = B.load_target_csv(Path(t["path"]))
        else:
            ns = argparse.Namespace(year=t["model_year"],
                                    weather_year=t["weather_year"],
                                    load_basis=t["load_basis"],
                                    overlays=t["overlays"])
            self.target, _ = B.load_target_resolve(ns)
        self.T = P.target_cell_energy(self.target)
        self.T["_y"] = self.target.demand_mw.to_numpy(dtype=np.float64)
        self.T["_dt"] = pd.DatetimeIndex(self.target.datetime_pst)
        self.mapped_bases = set(self.edges.base_id[self.edges.status == "ok"])
        self.mapped = self.nodes.base_id.isin(self.mapped_bases).to_numpy()
        self.guards = P.Guards(**{k: v for k, v in self.man["guards"].items()})
        self.axes = self.man["axes"]

    def raw_surface(self, source: str):
        if source == "stoch":
            run = self.axes.get("stochastic_run") or B.DEFAULT_STOCH_RUN
            src = B.PROJECTIONS / run / "substation_cell_mw.csv"
            return P.stoch_surface(src, self.axes.get("draw", "mean"), 3)
        return P.envelope_surface(B.PROFILES,
                                  self.axes.get("shape_col", "avg_load"))

    def base_shape(self, source: str):
        arr, units, meta = self.raw_surface(source)
        if source == "fleet":
            raw = P.fleet_surface(arr, self.T["block_of_cell"])[:, None]
            bases = ["__fleet__"]
        else:
            raw, bases, bm = P.base_envelope(arr, units, self.edges)
            meta.update(bm)
        sh, nm = P.energy_normalize(raw, self.T["Y"], self.T["block_of_cell"],
                                    self.guards.min_shape_net_gross)
        meta.update(nm)
        return sh, bases, meta

    def node_shape(self, source: str, common: str = "keep"):
        n = len(self.nodes)
        shape = np.ones((C.MONTHHOUR.n_cells, n))
        if source == "flat":
            return shape
        sh, bases, _ = self.base_shape(source)
        col = {b: i for i, b in enumerate(bases)}
        if source == "fleet":
            for i in range(n):
                if self.mapped[i]:
                    shape[:, i] = sh[:, 0]
        else:
            for i, b in enumerate(self.nodes.base_id):
                if self.mapped[i] and b in col:
                    shape[:, i] = sh[:, col[b]]
        if common == "strip" and self.mapped.any():
            _, l, _ = P.node_level_shares(self.nodes,
                                          self.axes["negative_nodes"],
                                          self.guards)
            sub, _ = P.strip_common(shape[:, self.mapped], l[self.mapped, :],
                                    self.T["Y"], self.T["block_of_cell"],
                                    self.guards.min_shape_net_gross)
            shape[:, self.mapped] = sub
        return shape


# ---------------------------------------------------------------------------

def section_a(cx: Ctx) -> None:
    head("A. Target", "docs/approach3_proportional.md 'Target'; README")
    T, y = cx.T, cx.T["_y"]
    t = cx.man["target"]
    say(f"source                      {t['source']}")
    for k in ("model_year", "weather_year", "load_basis", "overlays", "path"):
        if k in t:
            say(f"{k:<28}{t[k]}")
    say(f"hours                       {len(y):,}")
    say(f"annual energy               {y.sum() / 1e6:.3f} TWh")
    say(f"peak                        {y.max():,.1f} MW")
    say(f"minimum                     {y.min():,.1f} MW")
    for b, bn in enumerate(BLOCKS):
        m = T["block_of_cell"] == b
        Y = T["Y"][m]
        say(f"  {bn:<8} energy {Y.sum() / 1e6:8.3f} TWh   cells {m.sum():3d}   "
            f"Y(c) min {Y.min():,.0f} max {Y.max():,.0f} MWh  "
            f"ratio {Y.max() / Y.min():.3f}")
    say("")
    say("Y(c) dispersion is why the normalization must be target-energy")
    say("weighted: within one block it varies by month day-count (28 vs 31,")
    say("~11%) AND by the diurnal swing, so an unweighted cell mean is a")
    say("different quantity. Section B measures the resulting error.")


def section_b(cx: Ctx) -> None:
    head("B. THE NORMALIZATION NUMBER",
         "CLAUDE.md Approach 3 rule 3; docs 'Why energy-weighted'; "
         "skill approach3-proportional")
    T = cx.T
    source = cx.axes["shape_source"]
    arr, units, _ = cx.raw_surface(source if source != "flat" else "envelope")
    raw, bases, _ = P.base_envelope(arr, units, cx.edges)
    _, l, _ = P.node_level_shares(cx.nodes, cx.axes["negative_nodes"], cx.guards)
    col = {b: i for i, b in enumerate(bases)}

    def devs(shape_base):
        shape = np.ones((C.MONTHHOUR.n_cells, len(cx.nodes)))
        for i, b in enumerate(cx.nodes.base_id):
            if cx.mapped[i] and b in col:
                shape[:, i] = shape_base[:, col[b]]
        S, _ = P.node_shares(l, shape, cx.mapped, np.column_stack(
            [cx.nodes.winter_load, cx.nodes.summer_load]), T["Y"],
            T["block_of_cell"], "slack",
            P.Guards(max_slack_amplification=1e9, min_slack_shape=-np.inf))
        r = P.realized_energy_shares(S, T["Y"], T["block_of_cell"])
        d = np.abs(r - l)
        return d.max(), np.median(d[d > 0]) if (d > 0).any() else 0.0

    good, _ = P.energy_normalize(raw, T["Y"], T["block_of_cell"],
                                 cx.guards.min_shape_net_gross)
    bad = np.ones_like(raw)
    for b in range(2):
        m = T["block_of_cell"] == b
        mu = raw[m, :].mean(axis=0)
        nz = mu != 0
        bad[np.ix_(m, nz)] = raw[np.ix_(m, nz)] / mu[nz]

    gmax, gmed = devs(good)
    bmax, bmed = devs(bad)
    say(f"target-energy-weighted block mean (CORRECT, what Approach 3 uses)")
    say(f"   max |realized energy share - l_n|   {gmax:.3e}  "
        f"({gmax * 100:.6f} pp)")
    say(f"   median over nonzero                 {gmed:.3e}")
    say(f"unweighted block mean (what external_loads.normalized_shapes gives)")
    say(f"   max |realized energy share - l_n|   {bmax:.3e}  "
        f"({bmax * 100:.4f} pp)")
    say(f"   median over nonzero                 {bmed:.3e}  "
        f"({bmed * 100:.4f} pp)")
    say("")
    say(f"=> the unweighted normalization is off by {bmax * 100:.4f} pp at worst.")
    say("   The error is not random: a substation whose shape tracks the")
    say("   statewide diurnal pattern has a Y-weighted mean ABOVE its")
    say("   unweighted one, so it overshoots its energy and the slack silently")
    say("   absorbs the difference. That is the failure the design exists to")
    say("   prevent, which is why normalized_shapes must NOT be reused here.")


def section_c(cx: Ctx) -> None:
    head("C. SLACK FEASIBILITY MATRIX",
         "docs guard table; README; skill. CHOOSE THE SHIPPED DEFAULT FROM "
         "THIS TABLE -- do not pick it in advance")
    T = cx.T
    levels = np.column_stack([cx.nodes.winter_load, cx.nodes.summer_load])
    loose = P.Guards(max_slack_amplification=1e9, min_slack_shape=-np.inf,
                     min_net_gross_unmapped=0.0)
    say(f"{'shape':<10}{'common':<8}{'negative':<13}"
        + "".join(f"{'lamM_' + b:<12}{'lamU_' + b:<12}{'A_' + b:<9}"
                  f"{'minSU_' + b:<10}" for b in BLOCKS) + "feasible")
    for source in ("envelope", "stoch", "fleet", "flat"):
        for common in ("keep", "strip"):
            if source == "flat" and common == "strip":
                continue
            try:
                shape = cx.node_shape(source, common)
            except Exception as e:
                say(f"{source:<10}{common:<8}{'-':<13}unavailable: "
                    f"{type(e).__name__}: {str(e)[:50]}")
                continue
            for mode in ("net-base", "zero", "participate"):
                try:
                    _, l, _ = P.node_level_shares(cx.nodes, mode, cx.guards)
                    _, sd = P.node_shares(l, shape, cx.mapped, levels, T["Y"],
                                          T["block_of_cell"], "slack", loose)
                except Exception as e:
                    say(f"{source:<10}{common:<8}{mode:<13}"
                        f"refused: {str(e)[:60]}")
                    continue
                cols = ""
                feas = True
                for b in BLOCKS:
                    A = sd[f"A_{b}"]
                    mn = sd[f"min_shape_U_{b}"]
                    cols += (f"{sd[f'lambda_M_{b}']:<12.4f}"
                             f"{sd[f'lambda_U_{b}']:<12.4f}{A:<9.3f}{mn:<10.3f}")
                    feas &= (A <= cx.guards.max_slack_amplification
                             and mn >= cx.guards.min_slack_shape)
                say(f"{source:<10}{common:<8}{mode:<13}{cols}"
                    f"{'YES' if feas else 'no'}")
    say("")
    say(f"thresholds: A(b) <= {cx.guards.max_slack_amplification}, "
        f"min shape_U >= {cx.guards.min_slack_shape}")
    say("A(b) is the largest multiple of its own seasonal level that any")
    say("unmapped node sees in one cell. 'flat' is the only PROVABLY safe")
    say("combination (A == 1 exactly); 'fleet' is NOT safe -- it has the same")
    say("pathology because every mapped node then shares one shape.")


def section_d(cx: Ctx) -> None:
    head("D. Negative-level inventory",
         "docs 'negative levels'; CLAUDE.md Approach 3 rule 4; summary.csv")
    lv = np.column_stack([cx.nodes.winter_load, cx.nodes.summer_load])
    say(f"nodes                       {len(cx.nodes):,}")
    say(f"bases                       {cx.nodes.base_id.nunique():,}")
    say(f"nodes negative in any block {int((lv < 0).any(axis=1).sum()):,}")
    for b, bn in enumerate(BLOCKS):
        neg = lv[:, b] < 0
        say(f"  {bn:<8} {int(neg.sum()):>6,} negative, "
            f"{np.abs(lv[neg, b]).sum() / np.abs(lv[:, b]).sum() * 100:6.3f}% "
            f"of gross level mass")
    net = cx.nodes.groupby("base_id")[["winter_load", "summer_load"]].sum()
    pos = (cx.nodes.assign(w=cx.nodes.winter_load.clip(lower=0),
                           s=cx.nodes.summer_load.clip(lower=0))
           .groupby("base_id")[["w", "s"]].sum())
    mixed = (net.winter_load != pos.w) | (net.summer_load != pos.s)
    say(f"bases mixing signs          {int(mixed.sum()):,}")
    say("")
    say("net-base vs zero: the cost of the mode choice")
    for b, (nc, pc) in enumerate((("winter_load", "w"), ("summer_load", "s"))):
        ok = net[nc] > 0
        diff = float(net.loc[ok, nc].sum() - pos.loc[ok, pc].sum())
        tot = float(pos.loc[ok, pc].sum())
        say(f"  {BLOCKS[b]:<8} base totals differ by {diff:+.4g} of {tot:.4g} "
            f"level units ({diff / tot * 100:+.4f}%)")
        bad = net[nc] <= 0
        say(f"           bases netting <= 0: {int(bad.sum()):,}, "
            f"discarded level mass {float(np.abs(net.loc[bad, nc]).sum()):.4g}")
    try:
        ap = pd.read_csv(cx.dir / "node_annual_peak.csv")
        say("")
        say(f"node-hours negative in the output "
            f"{int(ap.n_hours_negative.sum()):,} "
            f"over {len(ap):,} nodes; nodes ever negative "
            f"{int((ap.n_hours_negative > 0).sum()):,}")
        say(f"minimum node-hour {ap.min_mw.min():,.3f} MW")
    except FileNotFoundError:
        pass
    arr, units, meta = cx.raw_surface(cx.axes["shape_source"]
                                      if cx.axes["shape_source"] != "flat"
                                      else "envelope")
    say("")
    say(f"raw shape surface: {int((arr < 0).sum()):,} of {arr.size:,} "
        f"(unit, cell) values are negative")
    say("CLAUDE.md records 13,318 cells across 368 substations with negative")
    say("min_load, kept as-is as real BTM reverse flow. Approach 3 does NOT")
    say("clip them: per-cell conservation is unaffected (the slack absorbs")
    say("whatever M(c) is) and exactness depends only on the weighted mean.")
    say("This is a deliberate divergence from the GenX clip(lower=0) at")
    say("rescale_genx_demand.py:482 -- correct THERE because a negative weight")
    say("can drive a per-cell denominator through zero.")


def section_e(cx: Ctx) -> None:
    head("E. Shape-source comparison and the per-sibling payoff",
         "docs; skill; cross-ref approach2-stochastic skill")
    T = cx.T
    shapes = {}
    for s in ("envelope", "stoch", "fleet", "flat"):
        try:
            shapes[s] = cx.node_shape(s)
        except Exception as e:
            say(f"{s}: unavailable ({type(e).__name__})")
    _, l, _ = P.node_level_shares(cx.nodes, cx.axes["negative_nodes"],
                                  cx.guards)
    say(f"{'source':<12}{'cross-node share CV':<22}"
        f"{'max |M/lamM - 1|':<20}annual-energy Spearman vs envelope")
    from scipy.stats import spearmanr
    base_annual = None
    for s, sh in shapes.items():
        cv = float(np.nanmedian(np.nanstd(sh[:, cx.mapped], axis=1)
                                / np.abs(np.nanmean(sh[:, cx.mapped], axis=1))))
        worst = 0.0
        for b in range(2):
            m = T["block_of_cell"] == b
            lam = l[cx.mapped, b].sum()
            if lam:
                M = sh[m, :][:, cx.mapped] @ l[cx.mapped, b]
                worst = max(worst, float(np.abs(M / lam - 1).max()))
        ann = T["Y"] @ (l[None, :, 0] * sh)
        if s == "envelope":
            base_annual = ann
            sp = 1.0
        else:
            sp = spearmanr(base_annual, ann).statistic
        say(f"{s:<12}{cv:<22.4f}{worst:<20.4f}{sp:.4f}")
    say("")
    say("max |M(c)/lambda_M - 1| is the COMMON-MODE SURVIVAL number. Approach")
    say("3 normalizes per BLOCK, which is strictly weaker than the per-cell")
    say("renormalization the CATS deliverable uses: a global scalar like F/F*")
    say("still cancels, but a CELL-COMMON factor g(c) does NOT -- it survives")
    say("identically in every mapped shape, shifting M(c) and hence R(c), i.e.")
    say("reallocating energy within the block between the mapped fleet and the")
    say("slack. --shape-common strip is the surgical removal of exactly that.")
    say("")
    say("Per-sibling draws (the subname axis):")
    sib = (cx.nodes.groupby("base_id").size())
    say(f"  siblings per base: median {int(sib.median())}, max {int(sib.max())}, "
        f"bases with >1 sibling {int((sib > 1).sum()):,}")
    say("  within-base, WITHIN-BLOCK CV of the sibling share ratio:")
    say("    --sibling-draws off          exactly 0 by construction")
    say("                                 (siblings NEVER reorder; Spearman")
    say("                                 (h10,h18) within a base == 1.0)")
    say("    --sibling-draws independent  > 0; measured in test_approach3.py")
    say("                                 T15 and in the run's own shapes")
    say("  --sibling-sigma proportional: cv(k=0.1)/cv(k=0.9) = 1.0, because")
    say("    k_n CANCELS in the shape -- the feature buys the independent eps,")
    say("    NOT the sigma scaling (test T14 pins this).")
    say("  --sibling-sigma quadrature:   that ratio is sqrt(0.9/0.1) = 3.0, so")
    say("    a smaller sibling IS relatively noisier and k_n does not cancel.")


def section_f(cx: Ctx) -> None:
    head("F. Rounding and conservation at printed precision",
         "docs 'Conservation at printed precision'; CLAUDE.md rule 6")
    r = cx.man["realized"]
    dec = cx.axes["decimals"]
    n_h = cx.man["target"]["n_hours"]
    say(f"decimals                                 {dec}")
    say(f"max hourly conservation error (printed)  "
        f"{r['max_hourly_conservation_error_mw']:.3e} MW")
    say(f"max target grid-snap offset              "
        f"{r.get('max_target_grid_snap_mw', float('nan')):.4g} MW")
    say("  the invariant is against the GRID-SNAPPED target: an off-grid target")
    say("  is unreachable by construction, so the snap offset (up to half a")
    say("  grid step) is reported separately and is NOT a conservation break.")
    say(f"max per-node printed-vs-float annual     "
        f"{r['max_printed_vs_float_annual_mwh']:.4g} MWh")
    say(f"  bound n_hours * 0.5 * 10^-{dec}            "
        f"{r['printed_drift_bound_mwh']:.4g} MWh "
        f"({n_h:,} h) -- largest-remainder keeps it far below")
    say(f"max per-cell share deviation from 1      "
        f"{r['max_abs_cell_share_error']:.3e}")
    say(f"max seasonal energy share deviation      "
        f"{r['max_abs_energy_share_dev_pp']:.3e} pp")
    say("")
    say("round_to_printed gained allow_negative (default False = bit-for-bit).")
    say("The defect it fixes: the reclaim branch refuses to take a unit below")
    say("zero, so an ALL-NEGATIVE row needing a reclaim raises 'could not")
    say("reclaim', and even when it succeeds it biases every downward")
    say("adjustment onto the positive columns. test_approach3.py T4 exercises")
    say("both; deliverables/test_compact.py checks expand.py's copy against")
    say("the original every run.")


def section_g(cx: Ctx) -> None:
    head("G. Mapping coverage at both levels", "docs; README")
    e = cx.edges
    nodes = cx.nodes
    lv = np.column_stack([nodes.winter_load, nodes.summer_load])
    say(f"edges supplied               {len(e):,}")
    say(f"  resolved                   {int((e.status == 'ok').sum()):,}")
    say(f"  unresolved                 {int((e.status != 'ok').sum()):,}")
    for st, k in e.status[e.status != "ok"].value_counts().items():
        say(f"      {k:>5}  {st[:90]}")
    say(f"distinct substations used    "
        f"{e.loc[e.status == 'ok', 'substation_name'].nunique():,}")
    say(f"bases                        {nodes.base_id.nunique():,}")
    say(f"  mapped                     {len(cx.mapped_bases):,} "
        f"({len(cx.mapped_bases) / nodes.base_id.nunique() * 100:.1f}%)")
    say(f"nodes                        {len(nodes):,}")
    say(f"  mapped                     {int(cx.mapped.sum()):,} "
        f"({cx.mapped.sum() / len(nodes) * 100:.1f}%)")
    say("")
    say("mapped share of LEVEL mass (measured at the NODE level, since nodes")
    say("are what carry load):")
    for b, bn in enumerate(BLOCKS):
        gross = np.abs(lv[:, b]).sum()
        net = lv[:, b].sum()
        say(f"  {bn:<8} {np.abs(lv[cx.mapped, b]).sum() / gross * 100:6.2f}% "
            f"of gross, {lv[cx.mapped, b].sum() / net * 100:6.2f}% of net")
    say("")
    say("SAY IT PRECISELY: 100% of load is always allocated. What the mapping")
    say("raises is the share of load whose HOURLY SHAPE is measurement-driven.")
    say("The rest carries the slack residual shape, which is still an exact")
    say("share of its own seasonal energy.")


SECTIONS = {"A": section_a, "B": section_b, "C": section_c, "D": section_d,
            "E": section_e, "F": section_f, "G": section_g}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default=None,
                    help="run tag under data/processed/load_projection/approach3/")
    ap.add_argument("--sections", default="".join(SECTIONS),
                    help="comma-free letters, e.g. BC (default all)")
    args = ap.parse_args()

    tag = args.run
    if tag is None:
        avail = sorted(p.name for p in RUNS.glob("*") if p.is_dir())
        if not avail:
            raise SystemExit(f"no Approach 3 runs under {RUNS}; build one first")
        tag = avail[-1]
        print(f"(no --run given; using {tag})")
    cx = Ctx(tag)
    say(f"Approach 3 doc numbers -- run {tag}")
    say(f"git {cx.man.get('git_rev')}  axes {json.dumps(cx.axes)}")
    want = [s for s in args.sections.replace(",", "").upper() if s in SECTIONS]
    for s in want:
        SECTIONS[s](cx)
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text("\n".join(_LINES) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT_FILE}")


if __name__ == "__main__":
    main()
