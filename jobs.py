"""Async ingest jobs.

Fetching a race from FastF1 takes 1-2 minutes uncached and the upstream API
rate-limits at 500 calls/hour, so ingest cannot happen inside an HTTP request.
This module is the queue and worker pool that lets the API accept a request,
return immediately, and let the UI poll for completion.

The pieces map deliberately onto their AWS counterparts, so migrating swaps
implementations rather than reshaping the API:

    SQLite job table   ->  DynamoDB (job status + dedup)
    in-process queue   ->  SQS (with a dead-letter queue for repeated failures)
    worker threads     ->  Lambda consumers with reserved concurrency
    RateLimiter        ->  Step Functions Map with a concurrency cap

Rate limiting is the constraint that shapes everything. A single race costs
roughly 11 upstream calls, so 500/hour allows ~45 races/hour. We cap below that
and surface the wait rather than letting the worker collide with a 429 and burn
retries.
"""

from __future__ import annotations

import dataclasses
import queue
import sqlite3
import threading
import time
import traceback
import uuid

DB = None  # set in init()

# One race is ~11 upstream calls against a 500/hour limit. Stay clear of it:
# hitting the limit costs a failed job and a retry, which is worse than waiting.
RACES_PER_HOUR = 35

# Ingest is I/O bound but the upstream limit is the real constraint, so extra
# workers buy nothing. Mirrors a Lambda reserved concurrency of 1.
N_WORKERS = 1

MAX_ATTEMPTS = 3

_q: "queue.Queue[str]" = queue.Queue()
_started = False
_lock = threading.Lock()


@dataclasses.dataclass
class Job:
    id: str
    slug: str
    year: int
    round: int | None
    event: str
    state: str            # queued | running | done | failed
    attempts: int
    error: str | None
    created: float
    finished: float | None

    @property
    def age_s(self) -> float:
        return time.time() - self.created


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def init(db_path) -> None:
    global DB
    DB = str(db_path)
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                slug TEXT NOT NULL,
                year INTEGER NOT NULL,
                round INTEGER,
                event TEXT,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created REAL NOT NULL,
                finished REAL
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS jobs_slug ON jobs(slug)")
        c.execute("CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state)")
        # A job left 'running' by a crash would block its slug forever.
        c.execute("UPDATE jobs SET state='queued' WHERE state='running'")


def _row_to_job(r: sqlite3.Row) -> Job:
    return Job(id=r["id"], slug=r["slug"], year=r["year"], round=r["round"],
               event=r["event"], state=r["state"], attempts=r["attempts"],
               error=r["error"], created=r["created"], finished=r["finished"])


def get_job(job_id: str) -> Job | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _row_to_job(r) if r else None


def job_for_slug(slug: str) -> Job | None:
    """Any in-flight job for this race, newest first."""
    with _conn() as c:
        r = c.execute(
            "SELECT * FROM jobs WHERE slug=? AND state IN ('queued','running')"
            " ORDER BY created DESC LIMIT 1", (slug,)).fetchone()
    return _row_to_job(r) if r else None


def recent_jobs(limit: int = 25) -> list[Job]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT ?",
                         (limit,)).fetchall()
    return [_row_to_job(r) for r in rows]


def _set_state(job_id: str, state: str, error: str | None = None,
               bump: bool = False) -> None:
    with _conn() as c:
        c.execute(
            f"UPDATE jobs SET state=?, error=?,"
            f" attempts=attempts+{1 if bump else 0},"
            f" finished=? WHERE id=?",
            (state, error, time.time() if state in ("done", "failed") else None,
             job_id))


# --------------------------------------------------------------------------- #
# rate limiting
# --------------------------------------------------------------------------- #

def ingests_last_hour() -> int:
    cutoff = time.time() - 3600
    with _conn() as c:
        r = c.execute("SELECT COUNT(*) n FROM jobs WHERE state='done'"
                      " AND finished > ?", (cutoff,)).fetchone()
    return int(r["n"])


def rate_budget() -> dict:
    used = ingests_last_hour()
    return {"used": used, "limit": RACES_PER_HOUR,
            "remaining": max(0, RACES_PER_HOUR - used)}


# --------------------------------------------------------------------------- #
# submission
# --------------------------------------------------------------------------- #

def submit(year: int, rnd: int | None, event: str, slug: str) -> tuple[Job, bool]:
    """Queue an ingest. Returns (job, created_now).

    Deduplicates on slug: a second request for a race already queued or running
    returns the existing job rather than doing the work twice.
    """
    with _lock:
        existing = job_for_slug(slug)
        if existing:
            return existing, False
        job_id = uuid.uuid4().hex[:12]
        with _conn() as c:
            c.execute(
                "INSERT INTO jobs (id,slug,year,round,event,state,attempts,"
                "created) VALUES (?,?,?,?,?, 'queued', 0, ?)",
                (job_id, slug, year, rnd, event, time.time()))
        _q.put(job_id)
        job = get_job(job_id)
        assert job is not None
        return job, True


# --------------------------------------------------------------------------- #
# worker
# --------------------------------------------------------------------------- #

def _process(job: Job) -> None:
    import dataio
    import ingestcore

    if dataio.has_race(job.slug):
        _set_state(job.id, "done")          # someone else got there first
        return

    budget = rate_budget()
    if budget["remaining"] <= 0:
        # Re-queue rather than fail: the limit is a wait, not an error. Sleeping
        # briefly stops a hot loop when the queue is otherwise empty.
        time.sleep(30)
        _q.put(job.id)
        return

    _set_state(job.id, "running", bump=True)
    try:
        laps, meta = ingestcore.ingest_race(job.year, job.round, job.event)
        dataio.save_race(laps, meta)
        _set_state(job.id, "done")
    except Exception as exc:  # noqa: BLE001 - any upstream failure must be recorded
        current = get_job(job.id)
        attempts = current.attempts if current else MAX_ATTEMPTS
        msg = f"{type(exc).__name__}: {exc}"
        if attempts < MAX_ATTEMPTS and "RateLimit" in type(exc).__name__:
            # Upstream throttling is transient; back off and retry.
            _set_state(job.id, "queued", error=msg)
            time.sleep(60)
            _q.put(job.id)
        else:
            _set_state(job.id, "failed", error=msg)
            traceback.print_exc()


def _worker() -> None:
    while True:
        job_id = _q.get()
        try:
            job = get_job(job_id)
            if job and job.state in ("queued", "running"):
                _process(job)
        except Exception:  # noqa: BLE001 - a worker must never die
            traceback.print_exc()
        finally:
            _q.task_done()


def start_workers() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
    # Re-queue anything left over from a previous run.
    with _conn() as c:
        for r in c.execute("SELECT id FROM jobs WHERE state='queued'").fetchall():
            _q.put(r["id"])
    for i in range(N_WORKERS):
        threading.Thread(target=_worker, name=f"ingest-{i}", daemon=True).start()


def queue_depth() -> int:
    return _q.qsize()
