"""Season schedules, cached to disk.

The UI needs to offer every race in a season - including ones not yet ingested -
so the picker is driven by FastF1's schedule rather than by what happens to be in
the store. Schedules change rarely, so they are cached; a completed season is
cached indefinitely while the current season is refreshed periodically.
"""

from __future__ import annotations

import json
import pathlib
import time

CACHE = pathlib.Path(__file__).parent / "data" / "schedules"

# FastF1's timing data starts in 2018.
FIRST_YEAR = 2018

# A season still in progress gains rounds, so its cached schedule goes stale.
CURRENT_SEASON_TTL_S = 24 * 3600


def available_years() -> list[int]:
    return list(range(time.gmtime().tm_year, FIRST_YEAR - 1, -1))


def _path(year: int) -> pathlib.Path:
    return CACHE / f"{year}.json"


def _fresh(path: pathlib.Path, year: int) -> bool:
    if not path.exists():
        return False
    if year < time.gmtime().tm_year:
        return True  # completed season: schedule is final
    return (time.time() - path.stat().st_mtime) < CURRENT_SEASON_TTL_S


def season(year: int, force: bool = False) -> list[dict]:
    """Rounds in a season: [{round, event, date, is_past}]."""
    path = _path(year)
    if not force and _fresh(path, year):
        return json.loads(path.read_text())

    import fastf1  # imported lazily: only needed on a cache miss

    import ingestcore
    ingestcore.enable_cache()
    sched = fastf1.get_event_schedule(year, include_testing=False)

    now = time.time()
    out = []
    for _, ev in sched.iterrows():
        rnd = ev.get("RoundNumber")
        if not rnd or int(rnd) == 0:
            continue
        date = ev.get("EventDate")
        ts = None
        try:
            ts = float(date.timestamp())
        except Exception:  # noqa: BLE001 - missing/odd dates are non-fatal
            pass
        out.append({
            "round": int(rnd),
            "event": str(ev.get("EventName", f"Round {rnd}")),
            "location": str(ev.get("Location", "")),
            "date": None if ts is None else time.strftime("%Y-%m-%d", time.gmtime(ts)),
            # Only a race that has happened can be ingested.
            "is_past": bool(ts is not None and ts < now),
            "slug": ingestcore.race_slug(year, str(ev.get("EventName", ""))),
        })

    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1))
    return out
