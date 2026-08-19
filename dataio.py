"""Race loading and repair.

The Parquet files written by ingest.py are close to FastF1's raw lap table. Two
repairs are needed before modelling, and both belong here rather than in ingest
so the stored data stays faithful to the source:

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

import functools
import json
import pathlib

import pandas as pd

DATA = pathlib.Path(__file__).parent / "data"
CATALOG = DATA / "catalog.json"


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

    # Age must be positive and finite to be usable.
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


@functools.lru_cache(maxsize=1)
def catalog() -> list[dict]:
    return json.loads(CATALOG.read_text())


def race_slugs(dry_only: bool = False) -> list[str]:
    wet = {"INTERMEDIATE", "WET"}
    return [e["slug"] for e in catalog()
            if not (dry_only and set(e["compounds"]) & wet)]
