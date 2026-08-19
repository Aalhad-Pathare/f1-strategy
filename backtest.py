"""Backtest: do the engine's pit recommendations match what teams actually did?

For every driver in every dry race, walk the race lap by lap. At each lap the
engine sees only laps 1..N, so its recommendation is causal. We record the lap at
which it first says PIT_NOW, or the lap where the cost of stopping now falls
below a threshold, and compare that against the driver's real first stop.

Teams are not a perfect oracle - they react to safety cars, traffic and rivals
the engine cannot see - so agreement is evidence, not proof. But a systematic
bias (always too early, always too late) would show up clearly here.
"""

from __future__ import annotations

import argparse
import numpy as np

import dataio
import model
import strategy

# Once stopping costs less than this vs the optimal plan, the engine is
# effectively saying "the window is open".
WINDOW_COST_S = 1.0


def actual_first_stop(laps, driver: str) -> int | None:
    d = laps[(laps["Driver"] == driver) & laps["IsPitLap"]]
    if d.empty:
        return None
    # The in-lap is the stop; take the earliest stint transition.
    stints = laps[laps["Driver"] == driver]["Stint"].dropna().unique()
    if len(stints) < 2:
        return None
    first = laps[(laps["Driver"] == driver) & (laps["Stint"] == stints[0])]
    return int(first["LapNumber"].max()) if not first.empty else None


def engine_window(laps, slug: str, driver: str, total: int) -> int | None:
    """First lap where the engine says the pit window is open."""
    for lap in range(6, total - strategy.MIN_STINT):
        r = strategy.recommend(laps, slug, lap, driver)
        if r.action == "NO_ADVICE":
            continue
        if r.action == "PIT_NOW":
            return lap
        c = r.cost_of_pitting_now
        if c is not None and np.isfinite(c) and c <= WINDOW_COST_S:
            return lap
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--races", type=int, default=8)
    args = ap.parse_args()

    slugs = dataio.race_slugs(dry_only=True)[: args.races]
    rows = []

    for slug in slugs:
        laps = dataio.load_race(slug)
        total = int(laps["LapNumber"].max())
        drivers = sorted(laps["Driver"].dropna().unique())
        per_race = []
        for drv in drivers:
            actual = actual_first_stop(laps, drv)
            if actual is None or actual < 6:
                continue
            pred = engine_window(laps, slug, drv, total)
            if pred is None:
                continue
            per_race.append(pred - actual)
            rows.append((slug, drv, actual, pred, pred - actual))
        if per_race:
            a = np.array(per_race)
            print(f"{slug:24s} n={len(a):2d}  median error {np.median(a):+5.1f} laps  "
                  f"MAE {np.mean(np.abs(a)):4.1f}  within 5 laps: "
                  f"{100*np.mean(np.abs(a)<=5):3.0f}%")

    if not rows:
        print("no comparable stops found")
        return 1

    err = np.array([r[4] for r in rows])
    print(f"\n{'='*66}")
    print(f"drivers compared      : {len(err)}")
    print(f"median error          : {np.median(err):+.1f} laps "
          f"({'engine stops later' if np.median(err) > 0 else 'engine stops earlier'})")
    print(f"mean abs error        : {np.mean(np.abs(err)):.1f} laps")
    print(f"within 3 laps         : {100*np.mean(np.abs(err) <= 3):.0f}%")
    print(f"within 5 laps         : {100*np.mean(np.abs(err) <= 5):.0f}%")
    print(f"within 10 laps        : {100*np.mean(np.abs(err) <= 10):.0f}%")

    worst = sorted(rows, key=lambda r: -abs(r[4]))[:5]
    print(f"\nlargest disagreements:")
    for slug, drv, a, p, e in worst:
        print(f"  {slug:22s} {drv:>4}  actual lap {a:2d}  engine {p:2d}  ({e:+d})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
