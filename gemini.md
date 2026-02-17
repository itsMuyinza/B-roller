# GEMINI Project Map and State Tracking

## Project
- Name: APRT Script 3 Trigger Deployment
- Phase: Phase 5 - T (Trigger / Deployment)
- Last Updated: 2026-02-17
- Owner: APRT / Muyinza

## Data Schema (Data-First Rule)

### Raw Input Payload
```json
{
  "story_id": "string",
  "brand": "string",
  "title": "string",
  "style_reference_images": ["https://... or /local/path.png"],
  "character": {
    "name": "string",
    "character_model_prompt": "string",
    "consistency_notes": "string",
    "auto_discover_from_story": true
  },
  "generation": {
    "image_model": "google/nano-banana-pro/edit",
    "video_model": "wavespeed-ai/wan-2.2/image-to-video",
    "image_resolution": "1k|2k|4k",
    "image_output_format": "png|jpeg",
    "video_resolution": "720p",
    "video_duration_seconds": 5,
    "movement_amplitude": "auto|small|medium|large",
    "generate_audio": true,
    "bgm": true,
    "poll_interval_seconds": 5,
    "poll_timeout_seconds": 1200
  },
  "scenes": [
    {
      "scene_id": "string",
      "narration": "string",
      "image_prompt": "string",
      "motion_prompt": "string",
      "reference_images": ["optional URLs or local paths"]
    }
  ],
  "output": {
    "cloud_target": "supabase|cloudinary",
    "supabase_table": "aprt_story_payloads"
  },
  "character_identity": {
    "audit_enabled": true,
    "auto_reuse_saved_model": true,
    "sources": ["duckduckgo_web", "wikipedia", "wikimedia_commons"],
    "min_confidence_score": 0.6
  }
}
```

### Processed Output Payload (Cloud Payload)
```json
{
  "story_id": "string",
  "run_id": "string",
  "status": "completed|failed",
  "run": {
    "started_at": "ISO-8601 UTC",
    "ended_at": "ISO-8601 UTC"
  },
  "character_model": {
    "task_id": "string",
    "status": "succeeded|failed",
    "image_url": "https://...",
    "source": "generated|registry_reuse",
    "registry_id": "string|null",
    "identity_audit": {
      "target_name": "string",
      "status": "verified|needs_review|reused|failed",
      "score": 0.0,
      "selected_reference_images": ["https://..."],
      "selected_source_urls": ["https://..."],
      "review_reference_images": ["https://..."],
      "review_source_urls": ["https://..."],
      "candidates": [
        {
          "title": "string",
          "image_url": "https://...",
          "source_url": "https://...",
          "score": 0.0,
          "matched_tokens": 0
        }
      ]
    }
  },
  "scenes": [
    {
      "scene_id": "string",
      "narration": "string",
      "image": {
        "task_id": "string",
        "status": "succeeded|failed",
        "url": "https://..."
      },
      "video": {
        "task_id": "string",
        "status": "succeeded|failed",
        "url": "https://..."
      }
    }
  ],
  "cloud_transfer": {
    "provider": "supabase|cloudinary",
    "status": "success|failed",
    "destination": "table name or cloud asset URL"
  },
  "errors": [
    {
      "stage": "character|scene_image|scene_video|cloud_transfer",
      "message": "string"
    }
  ]
}
```

### Dashboard View Payload (UI Layer)
```json
{
  "triggers": {
    "github_workflow": {
      "name": "phase5-story3-trigger",
      "schedule_cron_utc": "0 15 * * *",
      "workflow_path": ".github/workflows/phase5_story3_trigger.yml"
    },
    "local_webhook": {
      "path": "/webhook",
      "listener_script": "tools/webhook_listener.py"
    },
    "local_cron_script": "tools/install_local_cron.sh"
  },
  "runs": [
    {
      "run_id": "string",
      "status": "running|completed|failed",
      "scene_count": 8,
      "started_at": "ISO-8601 UTC",
      "ended_at": "ISO-8601 UTC",
      "cloud_provider": "supabase|cloudinary",
      "cloud_status": "success|failed|pending",
      "cloud_destination": "string",
      "payload_path": "absolute local path"
    }
  ],
  "script_panel": {
    "script_path": "tools/config/script_3_voiceover.md",
    "script_text": "full scrollable script content"
  },
  "character_registry": {
    "count": 0,
    "items": [
      {
        "id": "string",
        "name": "string",
        "aliases": ["string"],
        "name_key": "string",
        "image_url": "https://...",
        "source_url": "https://...",
        "audit_score": 0.0,
        "last_used_at": "ISO-8601 UTC",
        "updated_at": "ISO-8601 UTC"
      }
    ]
  },
  "character_audit_state": {
    "target_name": "string",
    "status": "verified|needs_review|reused|failed|pending",
    "score": 0.0,
    "selected_reference_images": ["https://..."],
    "selected_source_urls": ["https://..."]
  }
}
```

### Scene Grid Record (Editable)
```json
{
  "scene_id": "string",
  "position": 1,
  "narration": "string",
  "image_prompt": "string",
  "motion_prompt": "string",
  "reference_images": ["string"],
  "image_status": "pending|running|completed|failed",
  "image_task_id": "string|null",
  "image_url": "string|null",
  "video_status": "pending|running|completed|failed",
  "video_task_id": "string|null",
  "video_url": "string|null",
  "last_error": "string|null",
  "updated_at": "ISO-8601 UTC"
}
```

### Character Registry Record
```json
{
  "id": "string",
  "name": "string",
  "name_key": "string",
  "aliases": ["string"],
  "image_url": "https://...",
  "source_url": "https://...",
  "source_label": "string",
  "audit_score": 0.0,
  "audit_status": "verified|needs_review|failed",
  "audit_log": {
    "target_name": "string",
    "status": "string",
    "score": 0.0,
    "candidates": []
  },
  "created_at": "ISO-8601 UTC",
  "updated_at": "ISO-8601 UTC",
  "last_used_at": "ISO-8601 UTC"
}
```

## Execution Architecture
- `tools/wavespeed_story_pipeline.py`
  - Character-first consistency generation.
  - Scene image generation with style + character references.
  - Scene video generation using WAN 2.2 image-to-video.
- `tools/cloud_transfer.py`
  - Cloud transfer to Supabase REST table.
  - Automatic fallback transfer to Cloudinary raw asset upload.
- `tools/run_phase5_trigger.py`
  - Single trigger entrypoint for cron/workflow/webhook execution.
- `tools/webhook_listener.py`
  - Listener endpoint with optional WaveSpeed signature verification.

## Automation Triggers
- Cloud Cron: `.github/workflows/phase5_story3_trigger.yml`
- Manual Run: `workflow_dispatch`
- Webhook Trigger: `repository_dispatch` event `phase5_story3_trigger`
- Local Cron bootstrap: `tools/install_local_cron.sh`

## Maintenance Log
- 2026-02-17:
  - Added Phase 5 trigger deployment scaffolding.
  - Added payload schema before coding (Data-First rule satisfied).
  - Added WaveSpeed image-to-video model path `wavespeed-ai/wan-2.2/image-to-video`.
  - Added character consistency flow: first character model, then scene generation using character model as reference.
  - Added cloud transfer with Supabase primary + Cloudinary fallback.
  - Added repair loop SOP notes under `architecture/`.
  - Linked full Script 3 voiceover source file at `tools/config/script_3_voiceover.md`.
  - Added Pinterest reference URL resolver (`og:image` extraction) to improve style-reference reliability.
  - Added local dashboard UI for Airtable-style trigger operations:
    - Backend: `dashboard/app.py`
    - Frontend: `dashboard/templates/index.html`, `dashboard/static/*`
    - Runbook: `architecture/dashboard_ui_runbook.md`
  - Upgraded dashboard to Airtable-like scene grid:
    - Inline prompt editing per scene.
    - Per-scene `Generate Image` and `Generate Video` actions.
    - Batch generation actions (`Generate Missing Images`, `Generate Missing Videos`).
  - Added character identity audit + registry reuse pipeline:
    - Multi-source audit (`duckduckgo_web`, `wikipedia`, `wikimedia_commons`) with confidence scoring.
    - Scrapes source pages for candidate images and verifies with token-match confidence scoring.
    - Persists audited/saved character models in `character_registry`.
    - Auto-binds existing character model when a matching person name appears in the story/script.
    - Added dashboard controls: `Audit Story Character`, `Auto-Load Saved Character`, registry panel, and audited candidate previews.
  - Added character registry backfill from existing completed character state so older runs become reusable automatically.
    - Character-model-first generation control.
    - Scrollable script editor panel with save support.
    - Scene jobs queue + full trigger jobs queue with log tail.
  - Added download features:
    - Per-scene image/video download endpoints.
    - Latest payload JSON download button.
    - Run-specific payload download from Recent Runs.
  - Improved dashboard UI polish with glassmorphic visual system.
  - Added responsive mobile scene-card mode (full edit + trigger + download actions without horizontal clipping).
  - Added polling guard to avoid overlapping refresh calls and hidden-tab churn.
  - Added serverless-safe dashboard bootstrap (`DASHBOARD_DATA_ROOT`) and Vercel entrypoint (`api/index.py` + `vercel.json`).
  - Added script-save fallback path (`.tmp/dashboard/script_override.md`) when source script path is read-only.
  - Added character configuration controls in dashboard UI:
    - Editable character name, character-model prompt, consistency notes, and style-reference URLs.
    - Effective prompt preview with explicit style guardrail.
  - Added prompt-guardrail enforcement:
    - Every scene image/motion prompt is normalized to include the story character name.
    - Added anti-anime style lock (`not anime`, `not Dragon Ball`, `not Goku`) to character and scene generation prompts.
  - Added serverless runtime compatibility fixes:
    - Scene jobs run inline on serverless to avoid background-thread loss.
    - Full trigger is disabled on serverless due read-only filesystem expectations in trigger scripts; UI now communicates this clearly.
  - Fixed live provider reconciliation:
    - Corrected WaveSpeed polling endpoint to `GET /api/v3/predictions/{task-id}/result`.
    - Added task-lookup fallback handling for legacy endpoint variants.
  - Fixed live video generation failures:
    - WAN 2.2 currently accepts only `duration=5` or `duration=8`.
    - Added duration normalization and changed default `video_duration_seconds` to `5`.
  - Improved scene-image trigger reliability:
    - Added character preflight for scene image generation in serverless mode.
    - If character model is missing, dashboard now starts character generation and returns a clear "wait for completion" message instead of silent failure.
  - Updated style guardrails for originality:
    - Removed explicit franchise-name leakage in generation guardrails.
    - Enforced original anime-cartoon style without copying recognizable copyrighted characters.
  - Added media preview fallbacks in dashboard UI:
    - Broken image/video URLs now render safe placeholders instead of broken media widgets.
  - Improved table usability:
    - Scene actions column is sticky in desktop table mode to reduce clipping/jank on narrower viewports.
  - Fixed simulated dry-run media preview handling to prevent broken-media rendering.
  - Replaced Pinterest short-link style reference with direct Cloudinary image URL for reliable style conditioning.
  - Validation run (dry): `python3 tools/run_phase5_trigger.py --dry-run` succeeded, scene_count=8.
  - Validation run (live transfer): cloud upload succeeded to Cloudinary fallback.
  - Observed error: Supabase target table missing (`PGRST205`, table `public.aprt_story_payloads` not found).
  - Added SQL bootstrap file: `architecture/supabase_payload_table.sql`.
  - Latest live payload destination:
    - `https://res.cloudinary.com/dxobnvy3p/raw/upload/v1771329150/aprt/payloads/aprt_payloads_20260217T115225Z-5049e0f7.json`

## Self-Annealing Repair Loop (Operationalized)
1. Analyze: capture stack trace from `.tmp/logs` and process stderr.
2. Patch: update Python scripts in `tools/`.
3. Test: rerun failed stage in isolation with same payload.
4. Update Architecture: append fix note in `architecture/wavespeed_api_learnings.md`.

## Completion Criteria (Global Payload Rule)
- Local artifacts can remain in `.tmp/` as intermediates.
- Run is complete only after processed payload is stored in cloud destination and transfer status is `success`.
