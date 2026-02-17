# APRT B-Roll Dashboard

Local Airtable-style board for scene-level prompt editing and media triggers.

## What it does
- Shows every Script 3 scene as a grid row.
- Lets you edit `narration`, `image_prompt`, and `motion_prompt` inline.
- Generates character model first for visual consistency.
- Includes editable character settings panel:
  - Character name
  - Character model prompt
  - Consistency notes
  - Style reference URLs
- Auto-normalizes scene prompts to include character name + cartoon style lock.
- Triggers image generation per scene.
- Triggers video generation per scene (requires scene image).
- Supports batch triggers for missing images/videos.
- Downloads:
  - Per-scene image and video (live assets only).
  - Latest payload JSON from the top action bar.
  - Any run payload from Recent Runs.
- Displays scene job queue + full trigger job queue + log tail.
- Includes a scrollable script editor panel.

## Start
```bash
cd "/Users/muyinza/Desktop/APRT/APRT - MARKETING FOR ARTISTS"
python3 dashboard/app.py
```

Open:
- `http://127.0.0.1:5055`

## Core API Endpoints
- `GET /api/overview`
- `GET /api/scenes`
- `PATCH /api/scenes/<scene_id>`
- `POST /api/scenes/<scene_id>/generate-image`
- `POST /api/scenes/<scene_id>/generate-video`
- `POST /api/scenes/generate-images`
- `POST /api/scenes/generate-videos`
- `GET /api/scene-jobs`
- `GET /api/character`
- `GET /api/character/config`
- `PATCH /api/character/config`
- `POST /api/character/generate`
- `GET /api/script`
- `PATCH /api/script`
- `GET /api/scenes/<scene_id>/download/<image|video>`
- `GET /api/runs/latest/download`
- `GET /api/runs/<run_id>/download`
- `GET /api/jobs`
- `GET /api/jobs/<job_id>/log`
- `POST /api/trigger`

## Scene Trigger Example
```json
{
  "dry_run": true
}
```

## Batch Example
```json
{
  "dry_run": false,
  "only_missing": true
}
```
