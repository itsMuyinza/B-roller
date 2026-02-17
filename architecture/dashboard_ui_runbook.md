# Dashboard UI Runbook

## Scope
- Airtable-like scene board for Script 3 generation.
- Inline prompt edits and scene-by-scene trigger control.
- Character-model-first consistency workflow.

## Start
```bash
python3 dashboard/app.py
```

## URL
- `http://127.0.0.1:5055`

## Board Workflow
1. Generate character model:
   - Click `Generate Character Model`.
   - Use `Character Consistency` panel to set character name, style references, and prompt lock before generation.
2. Edit prompts:
   - Update scene row prompts directly in the grid.
   - Click `Save Prompt` on that row.
3. Generate images:
   - Per-row: `Generate Image`
   - Batch: `Generate Missing Images`
4. Generate videos:
   - Per-row: `Generate Video`
   - Batch: `Generate Missing Videos`
5. Download outputs:
   - Per-row: `Download Image`, `Download Video` (enabled only for live assets).
   - Top bar: `Download Latest Payload`.
   - Recent Runs: click run id to download that run payload.
6. Monitor queues:
   - Scene Jobs queue for per-scene stages.
   - Full Trigger Jobs queue for full pipeline runs.

## Data Sources
- Scene records: `dashboard` SQLite table `scenes` in `.tmp/dashboard/dashboard.db`
- Scene jobs: `scene_jobs` table in `.tmp/dashboard/dashboard.db`
- Trigger jobs: `trigger_jobs` table in `.tmp/dashboard/dashboard.db`
- Payload outputs: `.tmp/phase5_story3/payload_*.json`
- Full trigger logs: `.tmp/logs/dashboard_trigger_*.log`
- Script source: `tools/config/script_3_voiceover.md`
- Character/prompt override source (serverless fallback): `.tmp/dashboard/payload_config_override.json`

## Failure / Repair Loop
1. Analyze:
   - Scene errors in `last_error` column in grid.
   - Queue errors in Scene Jobs table.
   - Full trigger logs in Trigger Jobs panel.
2. Patch:
   - Backend: `dashboard/app.py`
   - Frontend: `dashboard/static/app.js`, `dashboard/static/styles.css`
3. Test:
   - Re-run scene-level dry run from board.
4. Document:
   - Update `gemini.md` maintenance log and this runbook.

## Serverless Runtime Notes
- On Vercel serverless, background threads are not durable across requests.
- Scene image/video jobs run inline in serverless mode so row actions remain reliable.
- `Run Full Trigger` is disabled in serverless mode because trigger scripts expect writable project directories.
- Use scene-level generation buttons in Vercel, or run full trigger from local CLI/GitHub workflow.
