# F1 Tyre Strategy Engine — architecture

Companion to [`architecture.html`](architecture.html), which is the same content
as a click-through diagram. Open that file in a browser; this document stands on
its own if you would rather read.

The system answers one question: **given a race, a lap and a car, should it pit
now, and onto what?** Everything else exists to make that answer honest.

## Modes

| Mode | What it means |
|---|---|
| **Deployed (AWS)** | What runs at the public URL. A container on Lambda behind an API Gateway HTTP API, serving a fixed dataset baked into the image. Ingest is off. |
| **Local (dev)** | What runs on a developer machine. uvicorn serves the same FastAPI app, plus on-demand ingest: a queue, worker threads and a rate limiter that fetch races from FastF1. |

The application code is identical in both. Mangum adapts the ASGI app to
Lambda's event model, so nothing branches on environment except the
`F1_INGEST` flag.

## Components

| Node | Mode | Role |
|---|---|---|
| **Browser** | both | Static HTML and vanilla JS. Race picker, lap scrubber, per-car focus. |
| **API Gateway** | AWS | HTTP API `f1-strategy-http`. Public HTTPS endpoint; proxies everything to the function. |
| **Lambda / uvicorn** | both | The application. On AWS: container image, x86_64, 1536MB, 30s timeout. Locally: a uvicorn process. |
| **dataio.py** | both | Storage boundary. Reads Parquet, applies repairs, owns the catalog index. |
| **model.py** | both | Fuel-corrected degradation model. Ridge-regularised least squares over clean laps. |
| **strategy.py** | both | Pit-window search and undercut/overcut analysis. |
| **ECR** | AWS | Container registry. Deploy-time only — never touched during a request. |
| **CloudWatch** | AWS | Log group. The execution role grants this and nothing else. |
| **jobs.py** | local | SQLite job store, in-process queue, worker threads, 35-races/hour limiter. |
| **FastF1 API** | local | Upstream timing data. Rate-limited to 500 calls/hour (~11 per race). |

## Flow 1 — Strategy request

`GET /api/races/{race}/lap/{lap}/driver/{code}`

1. **Browser → API Gateway.** The user scrubs to a lap and clicks a car.
2. **API Gateway → Lambda.** The request is wrapped as a payload-v2.0 event;
   Mangum converts it to ASGI. Locally this step does not exist — uvicorn speaks
   ASGI directly.
3. **Lambda → dataio.** Parquet is read and two repairs applied: tyre age
   reconstructed from stint structure, and compounds serialised as the literal
   text `nan` coerced to missing so they cannot be fitted as a real compound.
4. **dataio → model.** *The causality boundary.* Only clean laps up to the
   requested lap are visible. Fuel burn is pinned to a physical prior until stint
   offsets across the field can identify it; degradation slopes are ridge-shrunk
   toward priors.
5. **model → strategy.** Every (pit lap, compound) plan is projected to the flag,
   respecting the two-compound rule. Pit loss is measured from this race's own
   in/out laps. Rivals within 25 seconds get an undercut, overcut or exposure
   margin.
6. **strategy → Lambda.** The recommendation is serialised. NaN is not valid
   JSON, so `cost_of_pitting_now` becomes `null` when no stop-now option exists.
7. **Lambda → CloudWatch.** Duration and memory are logged. (AWS only.)
8. **Lambda → Browser.** The UI paints the call, candidate plans and rival table.

## Flow 2 — On-demand ingest (local only)

`POST /api/ingest?year=&round=`

A FastF1 race load takes 1–2 minutes and the upstream API allows 500 calls/hour,
so this cannot happen inside a request.

1. **Browser → app.** The user picks a race that is not downloaded.
2. **app → jobs.** The job is queued, deduplicated on race slug. The API returns
   a job id immediately and the UI polls it.
3. **jobs → FastF1.** A worker checks the rate budget, then fetches. Upstream
   throttling is transient, so it backs off and retries, capped at three attempts.
4. **FastF1 → dataio.** Laps are normalised and written as Parquet. The index is
   rewritten via a temp file under a lock, since a worker can finish while the
   API is reading it.
5. **dataio → Browser.** The catalog is cached against the index file's mtime, so
   a race added by a worker appears without restarting the API.

Disabled in the deployed build (`F1_INGEST=off`): it needs writable storage and a
long-lived worker, and a read-only serverless function has neither. The UI reads
the flag and says so rather than offering a button that cannot work.

## Flow 3 — Build and deploy (AWS only)

1. **Build and push.** Attestations must be off. Buildx attaches provenance and
   SBOM by default, which turns the push into a manifest *list*; Lambda rejects
   that with an error naming the media type rather than the build flags.
   `--provenance=false --sbom=false` fixes it.
2. **Lambda resolves and caches.** It pulls once, resolves the tag to an
   immutable digest, and unpacks into its own store. A container is required at
   all because pandas, numpy and pyarrow alone exceed the 250MB zip limit.
3. **Expose through an HTTP API.** Not a Function URL: this account blocks public
   ones — every request 403s before invocation, with a provably correct config.
   CloudFront with Origin Access Control failed the same way. An HTTP API invokes
   through the `apigateway` principal instead.

## Why the causality boundary matters

A model fit on the whole race will "predict" pit stops perfectly and be worthless.
The engine is therefore handed a prefix: at lap N it sees laps 1..N and nothing
after. That is enforced structurally rather than by discipline — the strategy
search never receives the full dataset.

It is also what makes the backtest meaningful. Across 77 drivers in 8 dry races,
the engine's pit window lands a median of 1.0 laps earlier than the stop teams
actually made, with 84% within 10 laps.
