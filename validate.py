"""Out-of-sample validation for the degradation model.

Hand-tuning model constants against one race overfits to that race. The honest
test is causal and predictive: fit through lap N, then predict the *next* few
laps' times for each driver and measure the error. That is exactly what the
strategy engine relies on when it projects a stint forward, so it is the metric
worth optimising.

Secondary check: compound ordering. Physics says softer compounds degrade
faster, so deg(SOFT) >= deg(MEDIUM) >= deg(HARD). A fit that inverts this has
latched onto something other than tyre wear.
"""

from __future__ import annotations

import argparse
import json
import pathlib

import numpy as np
import pandas as pd

import dataio
import model

DATA = pathlib.Path(__file__).parent / "data"
HORIZON = 5           # laps ahead to predict
FIT_POINTS = (15, 20, 25, 30, 40, 50)


def race_errors(laps: pd.DataFrame, through: int) -> tuple[list[float], bool | None]:
    """Predict laps through+1..through+HORIZON; return abs errors and whether
    compound ordering is physically sane."""
    m = model.fit(laps, through)

    # Driver pace offsets must come from *seen* laps only - using future laps
    # would leak information the engine would not have.
    seen = laps[(laps["LapNumber"] <= through) & laps["IsCleanLap"]]
    if seen.empty:
        return [], None
    field_med = float(seen["LapTimeSec"].median())
    driver_off = (seen.groupby("Driver")["LapTimeSec"].median() - field_med).to_dict()

    future = laps[
        (laps["LapNumber"] > through)
        & (laps["LapNumber"] <= through + HORIZON)
        & laps["IsCleanLap"]
        & laps["Compound"].notna()
    ]

    errs = []
    for row in future.itertuples():
        comp = str(row.Compound).upper()
        if comp in model.WET_COMPOUNDS:
            continue
        off = driver_off.get(row.Driver)
        if off is None or not np.isfinite(row.LapTimeSec):
            continue
        pred = m.predict(comp, row.TyreAge, row.LapNumber) + off
        errs.append(abs(pred - row.LapTimeSec))

    # Ordering check across whichever dry compounds were actually fitted.
    order = None
    d = {c: v for c, v in m.deg_rate.items() if c in ("SOFT", "MEDIUM", "HARD")}
    if len(d) >= 2:
        want = [c for c in ("HARD", "MEDIUM", "SOFT") if c in d]
        vals = [d[c] for c in want]
        order = all(vals[i] <= vals[i + 1] + 1e-9 for i in range(len(vals) - 1))
    return errs, order


def evaluate(races: list[pathlib.Path]) -> dict:
    per_point: dict[int, list[float]] = {p: [] for p in FIT_POINTS}
    order_ok, order_tot = 0, 0

    for path in races:
        laps = dataio.load_race(path)
        if laps.empty:
            continue
        total = int(laps["LapNumber"].max())
        for p in FIT_POINTS:
            if p + HORIZON > total:
                continue
            errs, order = race_errors(laps, p)
            per_point[p].extend(errs)
            if order is not None:
                order_tot += 1
                order_ok += int(order)

    out = {"by_lap": {}, "order_pct": None, "overall_mae": None}
    allerrs = []
    for p, e in per_point.items():
        if e:
            out["by_lap"][p] = {"mae": float(np.mean(e)),
                                "p90": float(np.percentile(e, 90)),
                                "n": len(e)}
            allerrs.extend(e)
    if allerrs:
        out["overall_mae"] = float(np.mean(allerrs))
    if order_tot:
        out["order_pct"] = 100.0 * order_ok / order_tot
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max races (0 = all)")
    ap.add_argument("--grid", action="store_true", help="grid search constants")
    ap.add_argument("--dry-only", action="store_true", default=True)
    args = ap.parse_args()

    catalog = json.loads((DATA / "catalog.json").read_text())
    races = []
    for e in catalog:
        if args.dry_only and set(e["compounds"]) & model.WET_COMPOUNDS:
            continue
        if (DATA / f"{e['slug']}.parquet").exists():
            races.append(e["slug"])
    if args.limit:
        races = races[: args.limit]
    print(f"evaluating {len(races)} dry races\n")

    if not args.grid:
        r = evaluate(races)
        print(f"overall MAE : {r['overall_mae']:.4f} s")
        print(f"ordering OK : {r['order_pct']:.1f}%")
        for p, v in sorted(r["by_lap"].items()):
            print(f"  lap {p:3d}: MAE {v['mae']:.4f}  p90 {v['p90']:.3f}  n={v['n']}")
        return 0

    print(f"{'SPREAD':>7} {'RIDGE':>7} {'MAE':>8} {'ORDER%':>7}   per-lap MAE")
    best = None
    for spread in (3.0, 6.0, 9.0, 12.0):
        for ridge in (40.0, 150.0, 400.0, 1000.0):
            model.MIN_STINT_OFFSET_SPREAD = spread
            model.RIDGE_BASE = ridge
            r = evaluate(races)
            row = " ".join(f"{p}:{r['by_lap'][p]['mae']:.3f}"
                           for p in sorted(r["by_lap"]))
            print(f"{spread:7.1f} {ridge:7.0f} {r['overall_mae']:8.4f} "
                  f"{r['order_pct'] or 0:7.1f}   {row}")
            score = r["overall_mae"] - 0.002 * (r["order_pct"] or 0)
            if best is None or score < best[0]:
                best = (score, spread, ridge, r)
    print(f"\nbest: spread={best[1]} ridge={best[2]} "
          f"MAE={best[3]['overall_mae']:.4f} order={best[3]['order_pct']:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
