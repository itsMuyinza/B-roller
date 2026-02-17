# Phase 5 Trigger Runbook

## Scope
- Promote local-tested logic into cloud-triggered execution.
- Ensure payload lands in a cloud destination.
- Keep maintenance updates in `gemini.md`.

## Components
- Generator: `tools/wavespeed_story_pipeline.py`
- Cloud transfer: `tools/cloud_transfer.py`
- Trigger orchestration: `tools/run_phase5_trigger.py`
- Listener: `tools/webhook_listener.py`
- Cloud cron workflow: `.github/workflows/phase5_story3_trigger.yml`

## Trigger Paths
1. Cloud cron:
   - GitHub Actions scheduled run (`0 15 * * *` UTC).
2. Manual run:
   - `workflow_dispatch` in GitHub Actions.
3. External webhook:
   - POST `/webhook` to `tools/webhook_listener.py`.
4. Local cron:
   - `bash tools/install_local_cron.sh "0 9 * * *"`

## Standard Commands
```bash
# Dry run end-to-end
python3 tools/run_phase5_trigger.py --dry-run

# Live run (uses .env)
python3 tools/run_phase5_trigger.py

# Listener
python3 tools/webhook_listener.py --host 0.0.0.0 --port 8787
```

## Cloud Transfer Policy
- Primary target: Supabase table `aprt_story_payloads`.
- Fallback target: Cloudinary raw asset (`aprt/payloads` folder).
- Run is considered complete only when `cloud_transfer.status == "success"`.

## Supabase Preflight
1. Ensure table exists before relying on primary transfer path:
   - Apply `architecture/supabase_payload_table.sql` in Supabase SQL editor.
2. Verify with:
```bash
python3 tools/cloud_transfer.py --payload .tmp/phase5_story3/latest_payload.json --provider supabase --dry-run
```

## Incident Note
- 2026-02-17:
  - Error: `PGRST205` (`public.aprt_story_payloads` missing in schema cache).
  - Resolution: added SQL bootstrap and enabled Cloudinary fallback path to keep payload delivery unblocked.

## Failure Handling (Self-Annealing)
1. Analyze:
   - Check `.tmp/logs/phase5_trigger_*.log`.
   - Check payload error objects in `.tmp/phase5_story3/latest_payload.json`.
2. Patch:
   - Fix script in `tools/`.
3. Test:
   - Re-run with the same payload.
4. Update architecture:
   - Append finding to `architecture/wavespeed_api_learnings.md`.
