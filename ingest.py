"""Batch ingest CLI: pull whole seasons into the local store.

A thin wrapper over `ingestcore` (the fetch/normalise logic, shared with the
async worker) and `dataio.save_race` (the storage boundary). It deliberately owns
no data handling of its own: an earlier version duplicated normalisation and
wrote the index directly, which meant a batch run would overwrite metadata the
rest of the system had added.

For single races prefer the UI or POST /api/ingest, which queue a background job
and respect the upstream rate limit. This CLI is for bulk seeding.
"""

from __future__ import annotations

import argparse
import sys

import dataio
import ingestcore


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="+", default=[2024, 2025])
    ap.add_argument("--skip-existing", action="store_true", default=True)
    args = ap.parse_args()

    import fastf1

    ingestcore.enable_cache()
    total_ok = total_skip = total_fail = 0

    for year in args.years:
        print(f"\n=== {year} season ===", file=sys.stderr)
        try:
            schedule = fastf1.get_event_schedule(year, include_testing=False)
        except Exception as exc:  # noqa: BLE001
            print(f"  cannot load {year} schedule: {exc}", file=sys.stderr)
            continue

        for _, ev in schedule.iterrows():
            rnd = ev["RoundNumber"]
            name = ev["EventName"]
            if not rnd or int(rnd) == 0:
                continue

            slug = ingestcore.race_slug(year, name)
            if args.skip_existing and dataio.has_race(slug):
                total_skip += 1
                print(f"  SKIP {slug} (already present)")
                continue

            try:
                laps, meta = ingestcore.ingest_race(year, rnd, name)
                dataio.save_race(laps, meta)
                total_ok += 1
                print(f"  OK   {meta['slug']}: {meta['total_laps']} laps, "
                      f"{meta['n_drivers']} drivers, {meta['green_pct']}% green")
            except Exception as exc:  # noqa: BLE001 - one bad race must not stop the run
                total_fail += 1
                print(f"  FAIL {slug}: {type(exc).__name__}: {exc}", file=sys.stderr)

    print(f"\ningested {total_ok}, skipped {total_skip}, failed {total_fail}")
    print(f"store now holds {len(dataio.catalog())} races")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
