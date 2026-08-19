# F1 Tyre Strategy Engine

Replays a historical Formula 1 race lap by lap and recommends tyre strategy for
any car: when to pit, which compound to fit, and whether an undercut or overcut
is available against nearby rivals.

Recommendations are **causal**. At lap N the engine is handed only laps 1..N, so
a replayed call is a genuine decision rather than hindsight. This is enforced
structurally: the pace model is fit on a prefix of the race and the strategy
search never touches the full dataset.

## Running it

```bash
.venv/bin/uvicorn api:app --port 8022     # then open http://localhost:8022
```

Ingest more races (FastF1 rate-limits at 500 calls/hour):

```bash
.venv/bin/python ingest.py --years 2024 2025
```

## Layout

| File | Role |
|---|---|
| `ingestcore.py` | FastF1 session → normalised lap table (used by CLI and worker) |
| `ingest.py` | Batch CLI over whole seasons |
| `schedule.py` | Cached season schedules, so the picker can offer un-ingested races |
| `jobs.py` | Async ingest queue, worker pool, rate limiting |
| `dataio.py` | Storage boundary: load, repair, save, index |
| `model.py` | Fuel-corrected degradation model |
| `strategy.py` | Pit-window search, undercut/overcut analysis |
| `api.py` | FastAPI service |
| `static/index.html` | Front end (F1 palette, official team liveries) |
| `backfill_colors.py` | One-off: add liveries to races ingested before colours existed |
| `lambda_app.py` | Lambda entrypoint (Mangum ASGI adapter) |
| `Dockerfile.api` | Read-path container image |
| `deploy/` | preflight, deploy (Lambda + API Gateway), teardown |
| `validate.py` | Out-of-sample model validation |
| `backtest.py` | Recommendations vs. what teams actually did |

## On-demand ingest

The UI offers every race from 2018 to the current season. If a race is not in
the store, requesting it queues a background fetch and the UI polls until it is
ready — typically well under a minute.

This has to be asynchronous. A FastF1 race load takes 1–2 minutes uncached and
the upstream API rate-limits at **500 calls/hour** (~11 calls per race, so ~45
races/hour). No user waits that long on a dropdown, and an API gateway would
time out regardless. So the API accepts a request, returns a job id, and a
worker does the fetch.

The local implementation is deliberately shaped like its cloud counterpart, so
migrating swaps implementations rather than reshaping the API:

| Local | AWS |
|---|---|
| SQLite `jobs` table | DynamoDB (job status + dedup) |
| in-process queue | SQS + dead-letter queue |
| worker threads | Lambda consumers, reserved concurrency |
| `RateLimiter` (35 races/hour) | Step Functions Map with a concurrency cap |
| `data/*.parquet` | S3 |

Jobs deduplicate on race slug, retry upstream throttling with backoff, cap at 3
attempts, and reset to `queued` if the process dies mid-fetch so a crash cannot
strand a race.

Note that **Kafka does not fit this design.** Its justification — many consumers
reading one ordered lap stream — belongs to a live telemetry replay path, not to
on-demand batch ingest. Forcing it in here would be decoration.

## Deployment

Phase 1 ships the read path as a container image on Lambda behind a public
Function URL. Race data and season schedules are baked into the image — 47 races
is ~1.5MB of Parquet, cheaper and faster than an S3 round trip per request.

**Live:** https://mq1yvieau8.execute-api.us-east-1.amazonaws.com

```bash
bash deploy/preflight.sh     # checks credentials, docker, data, schedules
bash deploy/deploy-api.sh    # build, push to ECR, create/update the function
bash deploy/deploy-apigw.sh  # expose it through an HTTP API
bash deploy/teardown.sh      # remove everything so nothing keeps billing
```

### Why API Gateway rather than a Lambda Function URL

The obvious deployment is a public Function URL, and it does not work in this
account: every request returns 403 before the function is invoked, with a
provably correct `AuthType=NONE` config and resource policy. AWS has been
defaulting new accounts to block public function URLs.

Putting CloudFront in front with Origin Access Control — the pattern AWS
recommends instead — failed the same way. The diagnosis came from three
observations: a manually SigV4-signed request from an IAM user returned 200, the
function's log group showed that request but never a CloudFront one, and the
account is not in an Organization so no SCP explains it. The distinguishing
factor is that the IAM user is authorised by an *identity* policy while
CloudFront relies solely on the *resource* policy, which the account-level block
appears to override.

An HTTP API sidesteps function URLs entirely, invoking the function through
`lambda:InvokeFunction` with the `apigateway` service principal — the most
standard integration in AWS. Cold start ~0.33s, warm ~0.28s.

Prerequisites: AWS credentials (`aws configure`) and a reachable Docker daemon.
On WSL that means enabling WSL integration in Docker Desktop's settings.

**Cost.** Lambda's always-free tier covers 1M requests and 400,000 GB-seconds per
month. API Gateway gives 1M requests/month free for 12 months, then $1.00 per
million. ECR storage is about $0.05/month for a 500MB image once the 12-month
500MB allowance lapses; a lifecycle policy expires untagged images after 14 days,
which is what actually reclaims space since each push orphans the previous
`latest`. At portfolio traffic the whole thing is a few cents a month.

**The API image deliberately omits FastF1.** It pulls matplotlib, scipy, and
cryptography, none of which the read path uses. Schedules are pre-cached into the
image instead, and `schedule.py` falls back to that cache when a refresh is
impossible — which is always the case in Lambda, where the filesystem is
read-only. Only the ingest worker image needs FastF1.

On-demand download is switched off in this deployment (`F1_INGEST=off`), because
it needs writable storage and a long-lived worker. The UI detects this and says
so rather than offering a button that cannot work. Phase 2 restores it against S3
and SQS.

## The modelling problem

Lap time is modelled as

```
lap_time = base_pace(compound) + deg_rate(compound) × tyre_age
           − fuel_effect × laps_completed
```

**Fuel burn masks degradation.** Raw lap times get *faster* through a stint as
the car sheds ~50kg. At Zandvoort 2024 Sargeant's hard-tyre laps (~75.5s) are
quicker than his mediums (~77.5s) purely from fuel load. Fit naively, a model
concludes tyres improve with age.

**Fuel and tyre age are collinear early on.** Before anyone pits, `tyre_age` and
`laps_completed` are the same number, so the two effects cannot be separated. A
plain OLS fit at lap 20 returned a fuel effect of 0.094 s/lap (~2.5× physical
reality) and inverted the compound ordering. That is exactly the moment the
engine matters most — a first stop falls around laps 15–25 — so the naive fit is
worst when it is needed most.

Two fixes, both in `model.py`:

1. **Pin the fuel term** to a physical prior until stint offsets across the field
   vary enough to identify it (`std(lap − tyre_age) ≥ 9`).
2. **Ridge-regularise the slopes** toward physical priors, decaying as clean laps
   accumulate. Base pace is left unpenalised.

## Results

Out-of-sample validation over 39 dry races (`validate.py`) — fit through lap N,
predict laps N+1..N+5:

| | weak penalty | tuned |
|---|---|---|
| MAE | 1.032 s | **0.624 s** |
| compound ordering physically correct | 51% | **96%** |

Early-race fits went from the worst to the best (lap 15 MAE 0.568 s), which was
the point of the exercise.

**An uncomfortable finding:** MAE plateaus under heavier shrinkage, meaning
per-race degradation rates carry little reliable signal — the physical priors
predict about as well as the data does. What genuinely adapts per race is *base
pace*. The model is honest about this rather than claiming to learn degradation
curves per race.

### Backtest vs. real teams

77 drivers across 8 dry races (`backtest.py`), engine's pit window vs. actual
first stop:

- median error **−1.0 laps** (essentially unbiased)
- **64%** within 5 laps, **84%** within 10 laps
- best: Bahrain (MAE 1.9 laps, 93% within 5)

Teams are not a perfect oracle, so agreement is evidence rather than proof.

## Colours

Team and compound colours come from FastF1 per session rather than a hardcoded
map, because liveries change: Hamilton is Mercedes teal in 2024 and Ferrari red
in 2025, and Sainz goes Ferrari red to Williams blue. They are resolved at ingest
and stored with the race, then sent once per race rather than per lap.

The official palettes are designed for dark broadcast graphics, so the pale ones
(HARD is `#f0f0ec`, Haas `#b6babd`) disappear against a light background. The UI
darkens anything above a luminance threshold instead of substituting a different
colour, so the livery stays recognisable.

## Known limitations

- **No safety cars.** A stop under an SC is nearly free; the engine has no
  concept of one, which explains most large backtest disagreements.
- **No traffic model.** Rejoining into a train can erase an undercut. Track
  position is not valued, so recommendations are poor at Monaco-type circuits.
- **No rival reaction.** Optimises the driver's own race time; it does not
  simulate rivals responding, so it approximates rather than solves "highest
  finishing position".
- **Wet races decline to advise.** Crossover strategy is a track-drying problem,
  not a tyre-wear one. 6 of 45 ingested races are affected.
- **Pit loss falls back to 21 s** in 4 of 39 races that offer no representative
  green-flag stop (Monaco 2024's lap-1 red flag let the whole field change tyres,
  so every observation is a red-flag stop).
- **Some sessions lack tyre data entirely.** FastF1's timing-app feed carries
  stint, compound and tyre life together, and it is missing for 15 drivers across
  354 laps of Miami 2025. Those laps cannot be attributed to a compound, so the
  engine declines to advise on them rather than projecting a guessed tyre.
- **Degradation is linear.** Real tyres fall off a cliff; a linear fit will
  understate very long stints.

## Data

Ships with 46 races (2024 ×24, 2025 ×21, plus 2026 Japan as an on-demand test).
Any other race from 2018 onward is fetchable from the UI on request.

The last three 2025 rounds are absent because the bulk ingest hit FastF1's
500-calls/hour limit. They can now simply be requested from the picker.
