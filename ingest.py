"""Pull race sessions from FastF1 and normalise them to lap-level Parquet.

The output is the only thing downstream code reads: a flat, per-lap table with
tyre compound, stint, and gap information already resolved. Keeping ingestion
separate means the strategy engine never touches the FastF1 API, which matters
because the engine must only ever see laps up to the one it is deciding on.

Writes one Parquet per race plus a catalog.json manifest so the API and UI can
enumerate available races without guessing filenames.
"""

import argparse
import json
import pathlib
import sys
import traceback

import fastf1
import pandas as pd

CACHE_DIR = pathlib.Path(__file__).parent / ".fastf1_cache"
OUT_DIR = pathlib.Path(__file__).parent / "data"
CATALOG = OUT_DIR / "catalog.json"

# Columns we care about for tyre strategy. Deliberately narrow: telemetry-level
# data is huge and irrelevant to lap-scale pit decisions.
LAP_COLUMNS = [
    "Driver",
    "DriverNumber",
    "Team",
    "LapNumber",
    "LapTime",
    "Stint",
    "Compound",
    "TyreLife",
    "FreshTyre",
    "PitInTime",
    "PitOutTime",
    "Position",
    "TrackStatus",
    "IsAccurate",
]


def race_slug(year: int, event_name: str) -> str:
    clean = event_name.lower().replace(" grand prix", "").strip()
    clean = "".join(c if c.isalnum() or c == " " else "" for c in clean)
    return f"{year}_{clean.replace(' ', '-')}"


def load_session(year: int, rnd, session: str = "R"):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(str(CACHE_DIR))
    ses = fastf1.get_session(year, rnd, session)
    ses.load(laps=True, telemetry=False, weather=True, messages=True)
    return ses


def normalise_laps(ses) -> pd.DataFrame:
    laps = ses.laps[LAP_COLUMNS].copy()

    # LapTime as seconds is far easier to model than timedelta.
    laps["LapTimeSec"] = laps["LapTime"].dt.total_seconds()

    # Pit laps are in/out laps; their lap times include pit lane transit and
    # must be excluded from any degradation fit.
    laps["IsPitLap"] = laps["PitInTime"].notna() | laps["PitOutTime"].notna()

    # TrackStatus is a string of concatenated per-lap codes; '1' is all-green.
    # Anything else (yellow, SC, VSC, red) distorts pace and must not train the
    # degradation model.
    laps["IsGreen"] = laps["TrackStatus"].fillna("").astype(str).str.strip() == "1"

    # A lap is usable for degradation modelling only if it is a clean, green,
    # non-pit, accurate timed lap.
    laps["IsCleanLap"] = (
        laps["LapTimeSec"].notna()
        & laps["IsAccurate"].fillna(False)
        & laps["IsGreen"]
        & ~laps["IsPitLap"]
    )

    return laps.sort_values(["LapNumber", "Position"]).reset_index(drop=True)


def ingest_race(year: int, rnd, event_name: str) -> dict | None:
    """Ingest one race. Returns a catalog entry, or None if the race is
    unavailable (future round, cancelled event, sprint-only data gap)."""
    try:
        ses = load_session(year, rnd, "R")
        laps = normalise_laps(ses)
    except Exception as exc:  # noqa: BLE001 - one bad race must not kill the run
        print(f"  SKIP {year} r{rnd} {event_name}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return None

    if laps.empty or laps["LapNumber"].isna().all():
        print(f"  SKIP {year} r{rnd} {event_name}: no lap data", file=sys.stderr)
        return None

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = race_slug(year, event_name)
    laps.to_parquet(OUT_DIR / f"{slug}.parquet", index=False)

    drivers = (
        laps[["Driver", "DriverNumber", "Team"]]
        .dropna(subset=["Driver"])
        .drop_duplicates(subset=["Driver"])
        .sort_values("Driver")
        .to_dict("records")
    )

    entry = {
        "slug": slug,
        "year": year,
        "round": int(rnd) if str(rnd).isdigit() or isinstance(rnd, int) else None,
        "event": event_name,
        "total_laps": int(laps["LapNumber"].max()),
        "n_drivers": int(laps["Driver"].nunique()),
        "n_laps_rows": int(len(laps)),
        "n_clean_laps": int(laps["IsCleanLap"].sum()),
        "green_pct": round(100.0 * laps["IsGreen"].mean(), 1),
        "compounds": sorted(laps["Compound"].dropna().unique().tolist()),
        "drivers": drivers,
    }
    print(f"  OK   {slug}: {entry['total_laps']} laps, "
          f"{entry['n_drivers']} drivers, {entry['green_pct']}% green")
    return entry


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--years", type=int, nargs="+", default=[2024, 2025])
    args = p.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Preserve entries from prior runs so a partial re-run does not lose races.
    catalog: dict[str, dict] = {}
    if CATALOG.exists():
        catalog = {e["slug"]: e for e in json.loads(CATALOG.read_text())}

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
            if rnd == 0:
                continue
            entry = ingest_race(year, rnd, name)
            if entry:
                catalog[entry["slug"]] = entry
                # Write incrementally so a crash keeps completed work.
                CATALOG.write_text(
                    json.dumps(sorted(catalog.values(),
                                      key=lambda e: (e["year"], e["round"] or 0)),
                               indent=2)
                )

    races = sorted(catalog.values(), key=lambda e: (e["year"], e["round"] or 0))
    CATALOG.write_text(json.dumps(races, indent=2))
    print(f"\nCatalog: {len(races)} races -> {CATALOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
