# Dashboard UI Runbook

## Scope
- Provide a local UI for Trigger control, run visibility, and script review.
- Mimic Airtable-style grid behavior for fast scanning.

## Start Command
```bash
python3 dashboard/app.py
```

## URL
- `http://127.0.0.1:5055`

## Panels
1. Trigger Launcher:
   - Choose mode (`Dry Run` or `Live Run`).
   - Choose cloud provider strategy (`auto`, `supabase`, `cloudinary`).
   - Launch pipeline trigger.
2. Runs Table:
   - Displays `run_id`, run status, scene count, cloud transfer status and destination.
3. Trigger Queue:
   - Displays background jobs and completion status.
   - Job log tail visible on row click.
4. Script Viewer:
   - Full scrollable content from `tools/config/script_3_voiceover.md`.

## Data Sources
- Runs: `.tmp/phase5_story3/payload_*.json`
- Trigger jobs: `.tmp/dashboard/dashboard.db`
- Logs: `.tmp/logs/dashboard_trigger_*.log`
- Trigger definitions:
  - `.github/workflows/phase5_story3_trigger.yml`
  - `crontab -l` entries containing `run_phase5_trigger.py`

## Failure Recovery
1. If trigger launch fails, inspect:
   - `.tmp/logs/dashboard_trigger_<job_id>.log`
2. Patch backend in:
   - `dashboard/app.py`
3. Re-test by POST trigger from UI.
4. Update this runbook and `gemini.md` maintenance log with findings.
