#!/usr/bin/env python3
"""Phase 5 Story pipeline: character-first images + per-scene videos via WaveSpeed."""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import pathlib
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests


WAVESPEED_API_BASE = "https://api.wavespeed.ai/api/v3"
SUCCESS_STATUSES = {"succeeded", "completed", "success"}
FAIL_STATUSES = {"failed", "error", "canceled", "cancelled"}


class PipelineError(RuntimeError):
    """Raised for recoverable pipeline failures."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_env_file(path: pathlib.Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def ensure_dir(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: pathlib.Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def maybe_resolve_reference_url(url: str) -> str:
    if "pin.it/" not in url and "pinterest." not in url:
        return url
    try:
        response = requests.get(
            url,
            timeout=20,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        content_type = response.headers.get("Content-Type", "").lower()
        if content_type.startswith("image/"):
            return response.url
        if "text/html" in content_type:
            patterns = [
                r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            ]
            for pattern in patterns:
                match = re.search(pattern, response.text, flags=re.IGNORECASE)
                if match:
                    return html.unescape(match.group(1))
    except Exception:  # noqa: BLE001
        return url
    return url


def normalize_status(payload: Dict[str, Any]) -> str:
    candidates = [
        payload.get("status"),
        payload.get("state"),
        payload.get("result", {}).get("status") if isinstance(payload.get("result"), dict) else None,
        payload.get("data", {}).get("status") if isinstance(payload.get("data"), dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate.strip().lower()
    return "unknown"


def collect_urls(value: Any) -> List[str]:
    urls: List[str] = []
    if isinstance(value, str):
        if is_url(value):
            urls.append(value)
        return urls
    if isinstance(value, list):
        for item in value:
            urls.extend(collect_urls(item))
        return urls
    if isinstance(value, dict):
        for nested in value.values():
            urls.extend(collect_urls(nested))
    return urls


def extract_task_id(payload: Dict[str, Any]) -> Optional[str]:
    for key in ("id", "task_id", "prediction_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    if isinstance(payload.get("data"), dict):
        return extract_task_id(payload["data"])
    return None


def extract_error_message(payload: Dict[str, Any]) -> str:
    for key in ("error", "message", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            text = extract_error_message(value)
            if text:
                return text
    if isinstance(payload.get("data"), dict):
        return extract_error_message(payload["data"])
    return "Unknown provider error"


def choose_primary_url(urls: List[str], kind: str) -> str:
    if not urls:
        raise PipelineError(f"No output URL returned for {kind}.")
    ext_priority: List[str]
    if kind == "image":
        ext_priority = [".png", ".jpg", ".jpeg", ".webp"]
    else:
        ext_priority = [".mp4", ".mov", ".webm", ".mkv"]
    lowered = [u.lower() for u in urls]
    for ext in ext_priority:
        for idx, item in enumerate(lowered):
            if ext in item:
                return urls[idx]
    return urls[0]


class WaveSpeedClient:
    def __init__(self, api_key: str, timeout_sec: int = 90) -> None:
        self.api_key = api_key
        self.timeout_sec = timeout_sec

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def submit_task(self, model_path: str, input_payload: Dict[str, Any]) -> Dict[str, Any]:
        endpoint = f"{WAVESPEED_API_BASE}/{model_path}"
        body = {
            "enable_base64_output": False,
            "input": input_payload,
        }
        response = requests.post(endpoint, headers=self._headers(), json=body, timeout=self.timeout_sec)
        response.raise_for_status()
        payload = response.json()
        task_id = extract_task_id(payload)
        if not task_id:
            raise PipelineError(f"WaveSpeed did not return task id for {model_path}: {payload}")
        return payload

    def poll_task(self, task_id: str, poll_interval_sec: int, timeout_sec: int) -> Dict[str, Any]:
        endpoint = f"{WAVESPEED_API_BASE}/predictions/{task_id}"
        started = time.time()
        while True:
            response = requests.get(endpoint, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=self.timeout_sec)
            response.raise_for_status()
            payload = response.json()
            status = normalize_status(payload)
            if status in SUCCESS_STATUSES:
                return payload
            if status in FAIL_STATUSES:
                raise PipelineError(f"WaveSpeed task {task_id} failed: {extract_error_message(payload)}")
            if time.time() - started > timeout_sec:
                raise PipelineError(f"Polling timeout for task {task_id} after {timeout_sec}s.")
            time.sleep(max(1, poll_interval_sec))

    def upload_local_file(self, path: pathlib.Path) -> str:
        endpoint = f"{WAVESPEED_API_BASE}/media/upload/binary"
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        raw = path.read_bytes()

        # WaveSpeed binary upload can be raw bytes. If provider rejects, retry as multipart.
        response = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": content_type,
            },
            data=raw,
            timeout=self.timeout_sec,
        )
        if not response.ok:
            response = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {self.api_key}"},
                files={"file": (path.name, raw, content_type)},
                timeout=self.timeout_sec,
            )
        response.raise_for_status()
        payload = response.json()
        urls = collect_urls(payload)
        if not urls:
            raise PipelineError(f"Upload succeeded but no URL returned for {path}. Response: {payload}")
        return urls[0]


def validate_config(config: Dict[str, Any]) -> None:
    required_top = ["story_id", "style_reference_images", "character", "generation", "scenes"]
    for key in required_top:
        if key not in config:
            raise PipelineError(f"Missing required key '{key}' in payload config.")
    if not isinstance(config["scenes"], list) or not config["scenes"]:
        raise PipelineError("Payload config must include at least one scene.")
    if not isinstance(config["style_reference_images"], list) or not config["style_reference_images"]:
        raise PipelineError("At least one style reference image is required.")


def resolve_reference_images(
    refs: List[str],
    client: Optional[WaveSpeedClient],
    dry_run: bool,
) -> List[str]:
    resolved: List[str] = []
    for idx, ref in enumerate(refs):
        if not isinstance(ref, str) or not ref.strip():
            continue
        ref = ref.strip()
        if is_url(ref):
            resolved.append(maybe_resolve_reference_url(ref))
            continue
        path = pathlib.Path(ref).expanduser().resolve()
        if not path.exists():
            raise PipelineError(f"Reference image path does not exist: {path}")
        if dry_run:
            resolved.append(f"https://dry-run.local/reference/{idx}-{path.name}")
            continue
        if client is None:
            raise PipelineError("WaveSpeed client unavailable for local file upload.")
        resolved.append(client.upload_local_file(path))
    if not resolved:
        raise PipelineError("No valid style reference image URLs were resolved.")
    return resolved


def run_pipeline(config: Dict[str, Any], out_dir: pathlib.Path, dry_run: bool) -> Dict[str, Any]:
    validate_config(config)
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    started_at = utc_now_iso()

    generation = config["generation"]
    poll_interval = int(generation.get("poll_interval_seconds", 5))
    poll_timeout = int(generation.get("poll_timeout_seconds", 1200))
    image_model = generation.get("image_model", "google/nano-banana-pro/edit")
    video_model = generation.get("video_model", "wavespeed-ai/wan-2.2/image-to-video")

    api_key = os.getenv("WAVESPEED_API_KEY", "")
    client = None if dry_run else WaveSpeedClient(api_key=api_key)
    if not dry_run and not api_key:
        raise PipelineError("WAVESPEED_API_KEY is required for live generation.")

    ensure_dir(out_dir)
    state_path = out_dir / f"state_{run_id}.json"

    payload: Dict[str, Any] = {
        "story_id": config.get("story_id"),
        "run_id": run_id,
        "status": "running",
        "run": {
            "started_at": started_at,
            "ended_at": None,
        },
        "character_model": {},
        "scenes": [],
        "cloud_transfer": {
            "provider": config.get("output", {}).get("cloud_target", "supabase"),
            "status": "pending",
            "destination": None,
        },
        "errors": [],
    }
    voiceover_path_raw = config.get("voiceover_script_path")
    if isinstance(voiceover_path_raw, str) and voiceover_path_raw.strip():
        voiceover_path = pathlib.Path(voiceover_path_raw).expanduser()
        if not voiceover_path.is_absolute():
            voiceover_path = pathlib.Path.cwd() / voiceover_path
        if not voiceover_path.exists():
            raise PipelineError(f"voiceover_script_path does not exist: {voiceover_path}")
        payload["source_script"] = {
            "path": str(voiceover_path.resolve()),
            "text": voiceover_path.read_text(encoding="utf-8"),
        }

    def checkpoint() -> None:
        write_json(state_path, payload)

    try:
        style_refs = resolve_reference_images(config["style_reference_images"], client=client, dry_run=dry_run)
        character_prompt = config["character"]["character_model_prompt"]

        if dry_run:
            character_task_id = f"dry-character-{run_id}"
            character_url = f"https://dry-run.local/{run_id}/character.png"
        else:
            character_input = {
                "prompt": character_prompt,
                "images": style_refs,
                "resolution": generation.get("image_resolution", "1k"),
                "output_format": generation.get("image_output_format", "png"),
            }
            character_submit = client.submit_task(image_model, character_input)  # type: ignore[union-attr]
            character_task_id = extract_task_id(character_submit) or ""
            character_result = client.poll_task(character_task_id, poll_interval, poll_timeout)  # type: ignore[union-attr]
            character_urls = collect_urls(character_result.get("output", character_result))
            character_url = choose_primary_url(character_urls, kind="image")

        payload["character_model"] = {
            "task_id": character_task_id,
            "status": "succeeded",
            "image_url": character_url,
            "consistency_notes": config["character"].get("consistency_notes", ""),
        }
        checkpoint()

        for idx, scene in enumerate(config["scenes"]):
            scene_id = scene.get("scene_id", f"scene_{idx + 1:02d}")
            scene_refs = list(style_refs) + [character_url]
            extra_refs = scene.get("reference_images", [])
            if isinstance(extra_refs, list) and extra_refs:
                scene_refs.extend(resolve_reference_images(extra_refs, client=client, dry_run=dry_run))

            if dry_run:
                image_task_id = f"dry-image-{scene_id}-{run_id}"
                image_url = f"https://dry-run.local/{run_id}/{scene_id}.png"
                video_task_id = f"dry-video-{scene_id}-{run_id}"
                video_url = f"https://dry-run.local/{run_id}/{scene_id}.mp4"
            else:
                image_input = {
                    "prompt": scene["image_prompt"],
                    "images": scene_refs,
                    "resolution": generation.get("image_resolution", "1k"),
                    "output_format": generation.get("image_output_format", "png"),
                }
                image_submit = client.submit_task(image_model, image_input)  # type: ignore[union-attr]
                image_task_id = extract_task_id(image_submit) or ""
                image_result = client.poll_task(image_task_id, poll_interval, poll_timeout)  # type: ignore[union-attr]
                image_urls = collect_urls(image_result.get("output", image_result))
                image_url = choose_primary_url(image_urls, kind="image")

                video_input = {
                    "image": image_url,
                    "prompt": scene["motion_prompt"],
                    "duration": int(generation.get("video_duration_seconds", 6)),
                    "resolution": generation.get("video_resolution", "720p"),
                    "movement_amplitude": generation.get("movement_amplitude", "auto"),
                    "generate_audio": bool(generation.get("generate_audio", True)),
                    "bgm": bool(generation.get("bgm", True)),
                }
                video_submit = client.submit_task(video_model, video_input)  # type: ignore[union-attr]
                video_task_id = extract_task_id(video_submit) or ""
                video_result = client.poll_task(video_task_id, poll_interval, poll_timeout)  # type: ignore[union-attr]
                video_urls = collect_urls(video_result.get("output", video_result))
                video_url = choose_primary_url(video_urls, kind="video")

            payload["scenes"].append(
                {
                    "scene_id": scene_id,
                    "narration": scene.get("narration", ""),
                    "image": {
                        "task_id": image_task_id,
                        "status": "succeeded",
                        "url": image_url,
                    },
                    "video": {
                        "task_id": video_task_id,
                        "status": "succeeded",
                        "url": video_url,
                    },
                }
            )
            checkpoint()

        payload["status"] = "completed"
    except Exception as exc:  # noqa: BLE001
        payload["status"] = "failed"
        payload["errors"].append({"stage": "pipeline", "message": str(exc)})
        checkpoint()
        raise
    finally:
        payload["run"]["ended_at"] = utc_now_iso()
        checkpoint()

    final_path = out_dir / f"payload_{run_id}.json"
    latest_path = out_dir / "latest_payload.json"
    write_json(final_path, payload)
    write_json(latest_path, payload)
    payload["local_output_path"] = str(final_path)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run APRT Script 3 WaveSpeed story pipeline.")
    parser.add_argument(
        "--input",
        default="tools/config/script_3_hoodrat_payload.json",
        help="Path to raw input payload JSON.",
    )
    parser.add_argument(
        "--out-dir",
        default=".tmp/phase5_story3",
        help="Directory for intermediate and final payload artifacts.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without live API calls; emits simulated output URLs.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cwd = pathlib.Path.cwd()
    load_env_file(cwd / ".env")

    input_path = pathlib.Path(args.input).expanduser().resolve()
    out_dir = pathlib.Path(args.out_dir).expanduser().resolve()

    if not input_path.exists():
        raise SystemExit(f"Input payload file does not exist: {input_path}")

    config = json.loads(input_path.read_text(encoding="utf-8"))
    result = run_pipeline(config=config, out_dir=out_dir, dry_run=bool(args.dry_run))
    print(json.dumps(
        {
            "status": result["status"],
            "run_id": result["run_id"],
            "output_path": result.get("local_output_path", str(out_dir / "latest_payload.json")),
            "scene_count": len(result.get("scenes", [])),
        },
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
