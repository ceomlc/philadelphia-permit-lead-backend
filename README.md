# Philadelphia Permit Lead Backend

A local FastAPI service around the confirmed Philadelphia permit pipeline. It fetches public Carto permit data, applies the specified scoring and segment rules, deduplicates by property, separates routed and manual-review queues, persists each completed run in SQLite, and preserves `qualified_leads.csv` output.

## Start locally

Python 3.10 or newer is recommended.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Change `ADMIN_API_KEY` in `.env`, then start the API:

```bash
uvicorn backend.main:app --reload --env-file .env
```

The API is available at `http://127.0.0.1:8000`; interactive OpenAPI documentation is at `http://127.0.0.1:8000/docs`.

The integrated McCann dashboard is served by the same process:

- Team Lead Queue: `http://127.0.0.1:8000/`
- Manual Review Queue: `http://127.0.0.1:8000/admin`

The team page reads live routed leads and summary counts. The manual-review page asks for the value of `ADMIN_API_KEY` and sends it only to the protected backend endpoint for the current browser session.

Run the pipeline manually:

```bash
curl -X POST "http://127.0.0.1:8000/pipeline/run?days_back=1&limit=1000"
```

The run fetches permits, writes its qualified lead snapshot to SQLite, and refreshes `qualified_leads.csv`. Reads always use the newest successfully completed run, so a failed fetch does not replace the last good API snapshot.

The reusable pipeline also remains a standalone CSV-producing script:

```bash
python permit_lead_pipeline.py
```

## Endpoints

### `POST /pipeline/run`

Runs the fetch → score → dedup → route pipeline. Optional query parameters:

- `days_back`: non-negative number of days to fetch; default `1`
- `limit`: Carto safety cap from `1` to `1000`; default `1000`

It returns run counts and the CSV location:

```json
{
  "run_id": 1,
  "fetched_count": 120,
  "qualified_count": 18,
  "routed_count": 12,
  "manual_review_count": 6,
  "csv_path": "/absolute/path/to/qualified_leads.csv"
}
```

Only one pipeline run can execute in a service process at a time. A competing trigger returns HTTP `409`.

### `GET /leads` and `GET /leads/routed`

Returns the routed/team-queue leads from the latest completed run. `/leads` defaults to `?queue=routed`.

Each object follows the front-end contract:

```json
{
  "permit": "RP-2026-000001",
  "opa_account_num": "123456789",
  "address": "100 EXAMPLE ST",
  "owner": "EXAMPLE OWNER",
  "type_of_work": "Addition and/or Alteration",
  "status": "ISSUED",
  "segment": "Residential",
  "days_old": 1,
  "score": 75,
  "reasons": [
    "issued within last 30 days",
    "substantial project type"
  ],
  "contractor": "EXAMPLE BUILDER",
  "assigned_rep": "Residential Team",
  "permit_count_for_property": 1,
  "needs_manual_review": false
}
```

### `GET /leads/summary`

Returns presentation-safe totals for the latest successful run, including routed/manual-review counts, average score, and routed lead counts by team. It never exposes manual-review records.

### `GET /leads/manual-review`

Returns qualified leads without a contractor/contact source. These leads are deliberately not assigned: `assigned_rep` is `null` and `needs_manual_review` is `true`.

This endpoint is admin-only:

```bash
curl -H "X-Admin-Key: your-key" \
  http://127.0.0.1:8000/leads/manual-review
```

If `ADMIN_API_KEY` is missing, the endpoint fails closed with HTTP `503`. An absent or incorrect key returns HTTP `403`.

### Queue query parameter

The same split is available through `GET /leads?queue=...`:

- `routed`: team queue; no admin header required
- `manual_review`: manual-review queue; admin header required
- `all`: both queues; admin header required so manual-review data is not exposed to sales reps

### `GET /health`

Returns `{"status":"ok"}` when the API process is running.

## Optional daily schedule

Manual triggering is enabled immediately. To also run once daily, set:

```dotenv
SCHEDULER_ENABLED=true
SCHEDULER_HOUR_UTC=10
SCHEDULER_MINUTE_UTC=0
```

The in-process scheduler is appropriate for this local/single-process stage. Run one Uvicorn worker when it is enabled, otherwise every worker would own a scheduler. A later deployment can call the same `POST /pipeline/run` endpoint from an external scheduler.

## Persistence and files

- SQLite defaults to `data/permit_leads.sqlite3` and can be changed with `DATABASE_PATH`.
- CSV defaults to `qualified_leads.csv` and can be changed with `CSV_PATH`.
- Each run has status and counts in `pipeline_runs`; qualified results are stored in `leads` and linked to that run.
- CSV export is atomic, so readers do not see a partly written file.

## Extension points

No unavailable integration is mocked or invented:

- **Per-agent routing:** `route_lead` in `permit_lead_pipeline.py` is the team-routing boundary. It already receives the permit row and segment, so capacity/territory logic can replace its internals without changing scoring or API fields.
- **Contact enrichment:** enrichment can be inserted after fetch and before qualification/routing, allowing a resolved contractor, phone, or email to keep a lead out of manual review.
- **Follow Up Boss:** `PipelineRunner` is the orchestration boundary. A CRM adapter can consume the successfully persisted lead snapshot after a run without coupling API storage to Follow Up Boss.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests cover segment boundaries, segment-specific scoring, property deduplication, manual-review routing, SQLite persistence, CSV output, endpoint queue separation, and admin authorization.
