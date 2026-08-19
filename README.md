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
| `static/index.html` | Front end |
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
- **Degradation is linear.** Real tyres fall off a cliff; a linear fit will
  understate very long stints.

## Data

Ships with 46 races (2024 ×24, 2025 ×21, plus 2026 Japan as an on-demand test).
Any other race from 2018 onward is fetchable from the UI on request.

The last three 2025 rounds are absent because the bulk ingest hit FastF1's
500-calls/hour limit. They can now simply be requested from the picker.
