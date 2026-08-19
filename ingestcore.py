"""Reusable ingest: FastF1 session -> normalised lap table + metadata.

Split out of the ingest CLI so a worker process (locally a thread, on AWS a
Lambda consuming an SQS message) can ingest one race without pulling in
command-line handling.
"""

from __future__ import annotations

import pathlib

import fastf1
import pandas as pd

CACHE_DIR = pathlib.Path(__file__).parent / ".fastf1_cache"

# Columns we care about for tyre strategy. Deliberately narrow: telemetry-level
# data is huge and irrelevant to lap-scale pit decisions.
LAP_COLUMNS = [
    "Driver", "DriverNumber", "Team", "LapNumber", "LapTime", "Stint",
    "Compound", "TyreLife", "FreshTyre", "PitInTime", "PitOutTime",
    "Position", "TrackStatus", "IsAccurate",
]


def race_slug(year: int, event_name: str) -> str:
    clean = event_name.lower().replace(" grand prix", "").strip()
    clean = "".join(c if c.isalnum() or c == " " else "" for c in clean)
    return f"{year}_{clean.replace(' ', '-')}"


def enable_cache() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(str(CACHE_DIR))


def load_session(year: int, rnd, session: str = "R"):
    enable_cache()
    ses = fastf1.get_session(year, rnd, session)
    ses.load(laps=True, telemetry=False, weather=True, messages=True)
    return ses


def normalise_laps(ses) -> pd.DataFrame:
    laps = ses.laps[LAP_COLUMNS].copy()

    # LapTime as seconds is far easier to model than timedelta.
    laps["LapTimeSec"] = laps["LapTime"].dt.total_seconds()

    # Pit laps are in/out laps; their times include pit lane transit and must be
    # excluded from any degradation fit.
    laps["IsPitLap"] = laps["PitInTime"].notna() | laps["PitOutTime"].notna()

    # TrackStatus is a string of concatenated per-lap codes; '1' is all-green.
    # Anything else (yellow, SC, VSC, red) distorts pace.
    laps["IsGreen"] = laps["TrackStatus"].fillna("").astype(str).str.strip() == "1"

    laps["IsCleanLap"] = (
        laps["LapTimeSec"].notna()
        & laps["IsAccurate"].fillna(False)
        & laps["IsGreen"]
        & ~laps["IsPitLap"]
    )
    return laps.sort_values(["LapNumber", "Position"]).reset_index(drop=True)


def build_meta(year: int, rnd, event_name: str, laps: pd.DataFrame) -> dict:
    drivers = (
        laps[["Driver", "DriverNumber", "Team"]]
        .dropna(subset=["Driver"])
        .drop_duplicates(subset=["Driver"])
        .sort_values("Driver")
        .to_dict("records")
    )
    return {
        "slug": race_slug(year, event_name),
        "year": int(year),
        "round": int(rnd) if rnd is not None else None,
        "event": event_name,
        "total_laps": int(laps["LapNumber"].max()),
        "n_drivers": int(laps["Driver"].nunique()),
        "n_laps_rows": int(len(laps)),
        "n_clean_laps": int(laps["IsCleanLap"].sum()),
        "green_pct": round(100.0 * laps["IsGreen"].mean(), 1),
        "compounds": sorted(laps["Compound"].dropna().unique().tolist()),
        "drivers": drivers,
    }


class NoLapData(RuntimeError):
    """Raised when a session exists but carries no usable lap data."""


def ingest_race(year: int, rnd, event_name: str) -> tuple[pd.DataFrame, dict]:
    """Ingest one race. Raises on failure so the caller can record it."""
    ses = load_session(year, rnd, "R")
    laps = normalise_laps(ses)
    if laps.empty or laps["LapNumber"].isna().all():
        raise NoLapData(f"{year} r{rnd} {event_name}: session has no lap data")
    return laps, build_meta(year, rnd, event_name, laps)
