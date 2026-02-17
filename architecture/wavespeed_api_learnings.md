# WaveSpeed API Learnings

## Canonical Endpoints
- Image edit model:
  - `POST https://api.wavespeed.ai/api/v3/google/nano-banana-pro/edit`
- Image-to-video model (this project):
  - `POST https://api.wavespeed.ai/api/v3/wavespeed-ai/wan-2.2/image-to-video`
- Polling:
  - `GET https://api.wavespeed.ai/api/v3/predictions/{task-id}/result`
  - Legacy fallback (some docs/examples only): `GET .../predictions/{task-id}` may return `404`
- Binary upload:
  - `POST https://api.wavespeed.ai/api/v3/media/upload/binary`

## Required Header
- `Authorization: Bearer <WAVESPEED_API_KEY>`

## Known Input Constraints
- Nano Banana edit:
  - Requires `input.prompt` and `input.images[]`.
- WAN 2.2 image-to-video:
  - Requires `input.image` and `input.prompt`.
  - `duration` must be `5` or `8` (provider rejects `6` with HTTP 400).
  - Use `resolution=720p` for Shorts-ready clips.
  - Scene video generation must be blocked until scene image URL exists.

## Character Consistency Pattern
1. Generate character model image first.
2. For each scene image request, pass:
   - style reference image(s), plus
   - character model image URL.
3. Animate each scene image with WAN 2.2.

## Webhook Signature Verification
- Headers used by provider:
  - `webhook-id`
  - `webhook-timestamp`
  - `webhook-signature`
- Signature computation:
  - `HMAC_SHA256(secret_without_whsec_prefix, "{id}.{timestamp}.{raw_body}")`

## Repair Log Entries
- 2026-02-17:
  - Added robust output URL extraction because prediction payload structures may differ by model.
  - Added upload fallback path (raw binary first, multipart retry) to tolerate media upload format differences.
  - Added cloud transfer fallback (Supabase -> Cloudinary) to prevent payload sink failures from blocking completion.
  - Added Pinterest `og:image` resolver for style links so short links can be converted into model-usable image URLs.
  - Added generation prompt style guardrail to reduce unintended anime/Goku bias:
    - `not anime`, `not Dragon Ball`, `not Goku` appended to effective character + scene prompts.
  - Added character-name anchoring in effective prompts so each scene keeps the same named subject.
  - Fixed polling endpoint mismatch: switched provider reconciliation to `.../predictions/{task-id}/result` after `404` failures on `.../predictions/{task-id}`.
  - Added duration guardrail for WAN 2.2: normalize unsupported `video_duration_seconds` values (for example `6`) to `5`.
