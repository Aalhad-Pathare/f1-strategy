"""HTTP API for the tyre strategy engine.

Endpoints are keyed by (race, lap, driver) so the UI can scrub through a replay
and focus any car. Nothing here reaches past the requested lap - the engine is
handed a prefix of the race, which is what keeps a replayed recommendation
honest rather than hindsight.
"""

from __future__ import annotations

import dataclasses
import functools
import math
import pathlib
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

import dataio
import model
import strategy

app = FastAPI(title="F1 Tyre Strategy", version="1.0")

# The UI is served from the same origin in deployment, but CORS keeps local
# front-end development on a different port workable.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@functools.lru_cache(maxsize=8)
def _laps(slug: str):
    try:
        return dataio.load_race(slug)
    except FileNotFoundError:
        raise HTTPException(404, f"unknown race {slug!r}")


def _clean(obj: Any) -> Any:
    """Recursively make a value JSON-safe.

    NaN and Infinity are not valid JSON; `cost_of_pitting_now` is deliberately
    NaN when no stop-now option exists, so it must serialise as null rather than
    producing a payload the browser refuses to parse.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _clean(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else round(obj, 3)
    return obj


@app.get("/api/races")
def list_races():
    """Catalog of ingested races, newest season first."""
    out = []
    for e in dataio.catalog():
        out.append({
            "slug": e["slug"],
            "year": e["year"],
            "round": e["round"],
            "event": e["event"],
            "total_laps": e["total_laps"],
            "compounds": e["compounds"],
            "green_pct": e["green_pct"],
            # Wet races are surfaced but marked: the engine declines to advise.
            "is_wet": bool(set(e["compounds"]) & model.WET_COMPOUNDS),
        })
    return sorted(out, key=lambda r: (-r["year"], r["round"] or 0))


@app.get("/api/races/{slug}")
def race_detail(slug: str):
    laps = _laps(slug)
    entry = next((e for e in dataio.catalog() if e["slug"] == slug), None)
    if entry is None:
        raise HTTPException(404, f"unknown race {slug!r}")
    return {
        "slug": slug,
        "event": entry["event"],
        "year": entry["year"],
        "total_laps": int(laps["LapNumber"].max()),
        "compounds": entry["compounds"],
        "is_wet": bool(set(entry["compounds"]) & model.WET_COMPOUNDS),
        "drivers": entry["drivers"],
        "pit_loss": round(strategy.measure_pit_loss(laps, int(laps["LapNumber"].max())), 2),
    }


@app.get("/api/races/{slug}/lap/{lap}")
def lap_state(slug: str, lap: int):
    """Every car's state at a lap - the replay's standings view."""
    laps = _laps(slug)
    total = int(laps["LapNumber"].max())
    lap = max(1, min(lap, total))
    states = strategy.race_state(laps, lap)
    m = model.fit(laps, lap)
    return _clean({
        "slug": slug,
        "lap": lap,
        "total_laps": total,
        "model": {
            "fitted": m.fitted,
            "confidence": m.confidence,
            "fuel_effect": m.fuel_effect,
            "fuel_pinned": m.fuel_pinned,
            "deg_rate": m.deg_rate,
            "residual_std": m.residual_std,
            "is_wet_race": m.is_wet_race,
        },
        "cars": sorted((_clean(s) for s in states.values()),
                       key=lambda c: c["position"]),
    })


@app.get("/api/races/{slug}/lap/{lap}/driver/{code}")
def driver_recommendation(slug: str, lap: int, code: str):
    """Full strategy recommendation for one car at one lap."""
    laps = _laps(slug)
    total = int(laps["LapNumber"].max())
    lap = max(1, min(lap, total))
    rec = strategy.recommend(laps, slug, lap, code.upper())
    return _clean(rec)


@app.get("/healthz")
def healthz():
    return {"ok": True, "races": len(dataio.catalog())}


@app.get("/", include_in_schema=False)
def index():
    ui = pathlib.Path(__file__).parent / "static" / "index.html"
    if ui.exists():
        return FileResponse(ui)
    return {"service": "f1-tyre-strategy", "docs": "/docs"}
