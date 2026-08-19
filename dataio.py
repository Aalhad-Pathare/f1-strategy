"""Race storage, loading, and repair.

This is the storage boundary. Locally it is a directory of Parquet plus a JSON
index; the AWS version replaces the directory with S3 and the index with
DynamoDB, without changing the interface the API and engine use.

Two repairs are applied on load, and both belong here rather than in ingest so
the stored files stay faithful to the source:

1. `TyreLife` is missing for a meaningful number of laps in some sessions
   (Miami 2025 lacks it for 309 clean laps; Belgium and Australia 2025 for
   dozens). Those laps are otherwise fine, so rather than discard them we
   reconstruct age from stint structure: within one driver's stint, tyre age
   advances one lap at a time, so age = lap - first_lap_of_stint + 1, anchored
   to whatever real TyreLife values the stint does have.

2. `IsCleanLap` must additionally require a finite tyre age, since a lap with
   unknown age cannot train a degradation model.

Reconstruction understates age only when a stint starts on a scrubbed tyre
(typically the grid tyre from qualifying), so `TyreAgeExact` records which laps
carry a measured value.
"""

from __future__ import annotations

import json
import pathlib
import threading

import pandas as pd

DATA = pathlib.Path(__file__).parent / "data"
INDEX = DATA / "catalog.json"

_lock = threading.Lock()
_cache: tuple[float, list[dict]] | None = None


# --------------------------------------------------------------------------- #
# index
# --------------------------------------------------------------------------- #

def catalog() -> list[dict]:
    """Ingested races, cached against the index file's mtime.

    An mtime check rather than a plain memo: a worker thread can add a race at
    any time, and a stale cache would hide it from the API until restart.
    """
    global _cache
    if not INDEX.exists():
        return []
    mtime = INDEX.stat().st_mtime
    if _cache is not None and _cache[0] == mtime:
        return _cache[1]
    entries = json.loads(INDEX.read_text())
    _cache = (mtime, entries)
    return entries


def entry(slug: str) -> dict | None:
    return next((e for e in catalog() if e["slug"] == slug), None)


def has_race(slug: str) -> bool:
    return (DATA / f"{slug}.parquet").exists() and entry(slug) is not None


def race_slugs(dry_only: bool = False) -> list[str]:
    wet = {"INTERMEDIATE", "WET"}
    return [e["slug"] for e in catalog()
            if not (dry_only and set(e["compounds"]) & wet)]


def save_race(laps: pd.DataFrame, meta: dict) -> None:
    """Persist a race and add it to the index.

    The index is rewritten under a lock and via a temporary file: workers can
    finish concurrently, and a half-written index would break the API.
    """
    DATA.mkdir(parents=True, exist_ok=True)
    slug = meta["slug"]
    laps.to_parquet(DATA / f"{slug}.parquet", index=False)

    with _lock:
        entries = {e["slug"]: e for e in catalog()}
        entries[slug] = meta
        ordered = sorted(entries.values(),
                         key=lambda e: (e["year"], e["round"] or 0))
        tmp = INDEX.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(ordered, indent=1))
        tmp.replace(INDEX)
        global _cache
        _cache = None


# --------------------------------------------------------------------------- #
# loading + repair
# --------------------------------------------------------------------------- #

def _repair_tyre_age(laps: pd.DataFrame) -> pd.DataFrame:
    laps = laps.copy()
    laps["TyreAgeExact"] = laps["TyreLife"].notna()

    grp = laps.groupby(["Driver", "Stint"], dropna=False)

    # Anchor: for each stint, the offset between lap number and known tyre age.
    # Median over the stint's measured laps is robust to a stray bad value.
    offset = (laps["TyreLife"] - laps["LapNumber"]).where(laps["TyreLife"].notna())
    laps["_off"] = offset.groupby([laps["Driver"], laps["Stint"]]).transform("median")

    # Stints with no measured age at all: assume the stint began fresh on its
    # first lap, i.e. age 1 on the earliest lap of the stint.
    first_lap = grp["LapNumber"].transform("min")
    laps["_off"] = laps["_off"].fillna(1.0 - first_lap)

    laps["TyreAge"] = laps["TyreLife"].fillna(laps["LapNumber"] + laps["_off"])
    laps = laps.drop(columns=["_off"])

    laps.loc[laps["TyreAge"] < 1, "TyreAge"] = pd.NA
    laps["TyreAge"] = pd.to_numeric(laps["TyreAge"], errors="coerce")
    return laps


def load_race(slug: str) -> pd.DataFrame:
    path = DATA / f"{slug}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"no race data for {slug!r}")
    laps = _repair_tyre_age(pd.read_parquet(path))
    laps["IsCleanLap"] = (
        laps["IsCleanLap"].fillna(False)
        & laps["TyreAge"].notna()
        & laps["Compound"].notna()
    )
    return laps
