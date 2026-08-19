"""Add official team/compound colours to races ingested before colours existed.

Sessions are read from the local FastF1 cache, so this makes no upstream calls
and cannot trip the 500-calls/hour limit.
"""

from __future__ import annotations

import sys

import dataio
import ingestcore


def main() -> int:
    entries = dataio.catalog()
    todo = [e for e in entries if not e.get("colors", {}).get("drivers")]
    print(f"{len(entries)} races, {len(todo)} need colours")

    ok = fail = 0
    for e in todo:
        try:
            ses = ingestcore.load_session(e["year"], e["round"], "R")
            laps = dataio.load_race(e["slug"])
            colors = ingestcore.extract_colors(ses, laps)
            if not colors.get("drivers"):
                raise RuntimeError("no driver colours resolved")
            merged = dict(e)
            merged["colors"] = colors
            # save_race rewrites the index entry; reuse the stored parquet.
            dataio.save_race(dataio.load_race(e["slug"]), merged)
            ok += 1
            print(f"  OK   {e['slug']}: {len(colors['drivers'])} drivers")
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print(f"  FAIL {e['slug']}: {type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"\ndone: {ok} updated, {fail} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
