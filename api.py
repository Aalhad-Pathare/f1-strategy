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
import os
import pathlib
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

import dataio
import jobs
import model
import schedule
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


JOB_DB = pathlib.Path(os.getenv("F1_JOB_DB",
                                pathlib.Path(__file__).parent / "data" / "jobs.db"))

# On-demand ingest needs writable storage and a long-lived worker. A read-only
# serverless deployment has neither, so it is switched off there rather than
# offering the user a button that cannot work. Phase 2 re-enables it against S3
# and SQS.
INGEST_ENABLED = os.getenv("F1_INGEST", "on").lower() in ("1", "on", "true", "yes")


@app.on_event("startup")
def _startup() -> None:
    if not INGEST_ENABLED:
        return
    jobs.init(JOB_DB)
    jobs.start_workers()


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
        # Official liveries for this season, resolved at ingest. Sent once per
        # race rather than per lap: the map is static for the session.
        "colors": entry.get("colors", {}),
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


@app.get("/api/seasons")
def list_seasons():
    """Years the upstream source covers, newest first."""
    ingested: dict[int, int] = {}
    for e in dataio.catalog():
        ingested[e["year"]] = ingested.get(e["year"], 0) + 1
    return [{"year": y, "ingested": ingested.get(y, 0)}
            for y in schedule.available_years()]


def _rate() -> dict:
    """Ingest budget, or a disabled marker when ingest is off."""
    if not INGEST_ENABLED:
        return {"enabled": False, "used": 0, "limit": 0, "remaining": 0}
    return {"enabled": True, **jobs.rate_budget()}


@app.get("/api/seasons/{year}")
def season_rounds(year: int):
    """Every round in a season, with whether its data is ready.

    Driven by the upstream schedule rather than by the store, so the picker can
    offer races that have not been ingested yet - which is the point of the
    on-demand flow.
    """
    if year not in schedule.available_years():
        raise HTTPException(404, f"no schedule for {year}")
    try:
        rounds = schedule.season(year)
    except Exception as exc:  # noqa: BLE001 - upstream schedule fetch can fail
        raise HTTPException(502, f"schedule unavailable: {exc}")

    out = []
    for r in rounds:
        e = dataio.entry(r["slug"])
        job = (jobs.job_for_slug(r["slug"])
               if e is None and INGEST_ENABLED else None)
        out.append({
            **r,
            "ready": e is not None,
            "total_laps": e["total_laps"] if e else None,
            "is_wet": bool(set(e["compounds"]) & model.WET_COMPOUNDS) if e else None,
            "job": None if job is None else {"id": job.id, "state": job.state},
            # A race that has not happened cannot be fetched.
            "ingestable": bool(r["is_past"]) and e is None and INGEST_ENABLED,
        })
    return {"year": year, "rounds": out, "rate": _rate(),
            "ingest_enabled": INGEST_ENABLED}


@app.post("/api/ingest")
def request_ingest(year: int, round: int):
    """Queue a race for ingest and return its job.

    Returns immediately: the fetch takes 1-2 minutes upstream, so the UI polls
    the job rather than holding a request open.
    """
    if not INGEST_ENABLED:
        raise HTTPException(503, "on-demand download is disabled in this deployment")
    try:
        rounds = schedule.season(year)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"schedule unavailable: {exc}")

    r = next((x for x in rounds if x["round"] == round), None)
    if r is None:
        raise HTTPException(404, f"{year} has no round {round}")
    if not r["is_past"]:
        raise HTTPException(409, f"{r['event']} has not been run yet")
    if dataio.has_race(r["slug"]):
        return {"state": "done", "slug": r["slug"], "already_present": True}

    budget = jobs.rate_budget()
    job, created = jobs.submit(year, round, r["event"], r["slug"])
    return {
        "id": job.id, "slug": job.slug, "event": job.event,
        "state": job.state, "created": created,
        "queue_depth": jobs.queue_depth(), "rate": budget,
    }


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    if not INGEST_ENABLED:
        raise HTTPException(503, "on-demand download is disabled in this deployment")
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"unknown job {job_id!r}")
    return {
        "id": job.id, "slug": job.slug, "event": job.event, "year": job.year,
        "state": job.state, "attempts": job.attempts, "error": job.error,
        "age_s": round(job.age_s, 1),
        "ready": dataio.has_race(job.slug),
    }


@app.get("/api/jobs")
def list_jobs():
    if not INGEST_ENABLED:
        return {"enabled": False, "queue_depth": 0, "rate": _rate(), "jobs": []}
    return {
        "enabled": True,
        "queue_depth": jobs.queue_depth(),
        "rate": _rate(),
        "jobs": [{"id": j.id, "slug": j.slug, "state": j.state,
                  "attempts": j.attempts, "error": j.error,
                  "age_s": round(j.age_s, 1)} for j in jobs.recent_jobs()],
    }


@app.get("/healthz")
def healthz():
    return {"ok": True, "races": len(dataio.catalog()),
            "ingest_enabled": INGEST_ENABLED}


@app.get("/", include_in_schema=False)
def index():
    ui = pathlib.Path(__file__).parent / "static" / "index.html"
    if ui.exists():
        return FileResponse(ui)
    return {"service": "f1-tyre-strategy", "docs": "/docs"}
