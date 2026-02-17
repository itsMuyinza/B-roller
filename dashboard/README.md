# APRT Trigger Dashboard

Local Airtable-style dashboard for Phase 5 trigger operations.

## Features
- Runs table with payload status and cloud destination.
- Trigger launcher (`dry` / `live`, provider selection).
- Trigger queue with job status and log viewer.
- Full Script 3 panel with scrollable text.
- Trigger metadata panel (GitHub cron, local cron, webhook paths).

## Start
```bash
cd "/Users/muyinza/Desktop/APRT/APRT - MARKETING FOR ARTISTS"
python3 dashboard/app.py
```

Open:
- `http://127.0.0.1:5055`

## API Endpoints
- `GET /api/overview`
- `GET /api/runs`
- `GET /api/runs/<run_id>`
- `GET /api/jobs`
- `GET /api/jobs/<job_id>/log`
- `POST /api/trigger`

## Trigger Request Example
```json
{
  "dry_run": true,
  "provider": "auto"
}
```
