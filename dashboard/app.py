#!/usr/bin/env python3
"""APRT Phase 5 local dashboard (Airtable-style grid + editable prompt workflow)."""

from __future__ import annotations

import html
import json
import mimetypes
import os
import pathlib
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml
from flask import Flask, jsonify, render_template, request


ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP_DIR = ROOT / ".tmp"
RUN_DIR = TMP_DIR / "phase5_story3"
LOG_DIR = TMP_DIR / "logs"
DASH_DIR = TMP_DIR / "dashboard"
DB_PATH = DASH_DIR / "dashboard.db"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "phase5_story3_trigger.yml"
PAYLOAD_CONFIG_PATH = ROOT / "tools" / "config" / "script_3_hoodrat_payload.json"
DEFAULT_SCRIPT_PATH = ROOT / "tools" / "config" / "script_3_voiceover.md"

PYTHON_BIN = os.environ.get("PYTHON", "python3")
WAVESPEED_API_BASE = "https://api.wavespeed.ai/api/v3"
SUCCESS_STATUSES = {"succeeded", "completed", "success"}
FAIL_STATUSES = {"failed", "error", "canceled", "cancelled"}

ACTIVE_TRIGGER_JOBS: Dict[str, Dict[str, Any]] = {}
ACTIVE_TRIGGER_LOCK = threading.Lock()
ACTIVE_SCENE_JOBS: Dict[Tuple[str, str], Dict[str, Any]] = {}
ACTIVE_SCENE_LOCK = threading.Lock()
REF_CACHE: Dict[str, str] = {}

app = Flask(__name__, template_folder="templates", static_folder="static")


class DashboardError(RuntimeError):
    """Raised for user-visible failures."""


def utc_now() -> str:
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


def ensure_dirs() -> None:
    for path in (TMP_DIR, RUN_DIR, LOG_DIR, DASH_DIR):
        path.mkdir(parents=True, exist_ok=True)


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    ensure_dirs()
    with db_conn() as conn:
        conn.executescript(
            """
            create table if not exists trigger_jobs (
                id text primary key,
                requested_at text not null,
                finished_at text,
                mode text not null,
                provider text not null,
                status text not null,
                pid integer,
                exit_code integer,
                log_path text not null
            );

            create table if not exists scene_jobs (
                id text primary key,
                scene_id text,
                stage text not null,
                mode text not null,
                status text not null,
                requested_at text not null,
                finished_at text,
                task_id text,
                result_url text,
                error text
            );

            create table if not exists scenes (
                scene_id text primary key,
                position integer not null,
                narration text not null,
                image_prompt text not null,
                motion_prompt text not null,
                reference_images text not null default '[]',
                image_status text not null default 'pending',
                image_task_id text,
                image_url text,
                video_status text not null default 'pending',
                video_task_id text,
                video_url text,
                last_error text,
                updated_at text not null
            );

            create table if not exists metadata (
                key text primary key,
                value text not null,
                updated_at text not null
            );
            """
        )


def set_meta(key: str, value: str) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            insert into metadata(key, value, updated_at)
            values (?, ?, ?)
            on conflict(key) do update set value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, utc_now()),
        )


def get_meta(key: str, default: Optional[str] = None) -> Optional[str]:
    with db_conn() as conn:
        row = conn.execute("select value from metadata where key = ?", (key,)).fetchone()
    return row["value"] if row else default


def load_payload_config() -> Dict[str, Any]:
    if not PAYLOAD_CONFIG_PATH.exists():
        raise DashboardError(f"Missing payload config: {PAYLOAD_CONFIG_PATH}")
    return json.loads(PAYLOAD_CONFIG_PATH.read_text(encoding="utf-8"))


def get_generation_config() -> Dict[str, Any]:
    cfg = load_payload_config()
    generation = cfg.get("generation") if isinstance(cfg.get("generation"), dict) else {}
    return {
        "image_model": generation.get("image_model", "google/nano-banana-pro/edit"),
        "video_model": generation.get("video_model", "wavespeed-ai/wan-2.2/image-to-video"),
        "image_resolution": generation.get("image_resolution", "1k"),
        "image_output_format": generation.get("image_output_format", "png"),
        "video_resolution": generation.get("video_resolution", "720p"),
        "video_duration_seconds": int(generation.get("video_duration_seconds", 6)),
        "movement_amplitude": generation.get("movement_amplitude", "auto"),
        "generate_audio": bool(generation.get("generate_audio", True)),
        "bgm": bool(generation.get("bgm", True)),
        "poll_interval_seconds": int(generation.get("poll_interval_seconds", 5)),
        "poll_timeout_seconds": int(generation.get("poll_timeout_seconds", 1200)),
    }


def discover_script_path() -> pathlib.Path:
    if PAYLOAD_CONFIG_PATH.exists():
        try:
            payload = load_payload_config()
            raw = payload.get("voiceover_script_path")
            if isinstance(raw, str) and raw.strip():
                path = pathlib.Path(raw.strip()).expanduser()
                if not path.is_absolute():
                    path = ROOT / path
                return path.resolve()
        except Exception:
            pass
    return DEFAULT_SCRIPT_PATH.resolve()


def read_script_panel() -> Dict[str, Any]:
    script_path = discover_script_path()
    text = script_path.read_text(encoding="utf-8") if script_path.exists() else ""
    return {
        "script_path": str(script_path),
        "script_text": text,
    }


def write_script_text(new_text: str) -> Dict[str, Any]:
    script_path = discover_script_path()
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(new_text, encoding="utf-8")
    return read_script_panel()


def sync_scenes_from_payload() -> None:
    config = load_payload_config()
    scenes = config.get("scenes", [])
    if not isinstance(scenes, list):
        return

    with db_conn() as conn:
        for index, scene in enumerate(scenes, start=1):
            scene_id = str(scene.get("scene_id") or f"scene_{index:02d}")
            narration = str(scene.get("narration", "")).strip()
            image_prompt = str(scene.get("image_prompt", "")).strip()
            motion_prompt = str(scene.get("motion_prompt", "")).strip()
            ref_images = scene.get("reference_images", [])
            if not isinstance(ref_images, list):
                ref_images = []
            refs_json = json.dumps(ref_images, ensure_ascii=True)

            existing = conn.execute("select scene_id from scenes where scene_id = ?", (scene_id,)).fetchone()
            if existing is None:
                conn.execute(
                    """
                    insert into scenes (
                        scene_id, position, narration, image_prompt, motion_prompt,
                        reference_images, image_status, video_status, updated_at
                    ) values (?, ?, ?, ?, ?, ?, 'pending', 'pending', ?)
                    """,
                    (
                        scene_id,
                        index,
                        narration,
                        image_prompt,
                        motion_prompt,
                        refs_json,
                        utc_now(),
                    ),
                )
            else:
                conn.execute("update scenes set position = ? where scene_id = ?", (index, scene_id))


def parse_ref_images(raw: str) -> List[str]:
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def list_scenes() -> List[Dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute(
            """
            select scene_id, position, narration, image_prompt, motion_prompt,
                   reference_images, image_status, image_task_id, image_url,
                   video_status, video_task_id, video_url, last_error, updated_at
            from scenes
            order by position asc
            """
        ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["reference_images"] = parse_ref_images(item.get("reference_images", "[]"))
        out.append(item)
    return out


def get_scene(scene_id: str) -> Optional[Dict[str, Any]]:
    with db_conn() as conn:
        row = conn.execute(
            """
            select scene_id, position, narration, image_prompt, motion_prompt,
                   reference_images, image_status, image_task_id, image_url,
                   video_status, video_task_id, video_url, last_error, updated_at
            from scenes
            where scene_id = ?
            """,
            (scene_id,),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["reference_images"] = parse_ref_images(item.get("reference_images", "[]"))
    return item


def update_scene_fields(scene_id: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not fields:
        return get_scene(scene_id)

    allowed = {
        "narration",
        "image_prompt",
        "motion_prompt",
        "reference_images",
        "image_status",
        "image_task_id",
        "image_url",
        "video_status",
        "video_task_id",
        "video_url",
        "last_error",
    }
    updates: Dict[str, Any] = {}
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "reference_images" and isinstance(value, list):
            updates[key] = json.dumps(value, ensure_ascii=True)
        else:
            updates[key] = value

    if not updates:
        return get_scene(scene_id)

    updates["updated_at"] = utc_now()
    set_sql = ", ".join(f"{key} = ?" for key in updates.keys())
    values = list(updates.values()) + [scene_id]
    with db_conn() as conn:
        conn.execute(f"update scenes set {set_sql} where scene_id = ?", values)
    return get_scene(scene_id)


def insert_scene_job(record: Dict[str, Any]) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            insert into scene_jobs(id, scene_id, stage, mode, status, requested_at, task_id, result_url, error)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["id"],
                record.get("scene_id"),
                record["stage"],
                record["mode"],
                record["status"],
                record["requested_at"],
                record.get("task_id"),
                record.get("result_url"),
                record.get("error"),
            ),
        )


def update_scene_job(
    job_id: str,
    *,
    status: str,
    task_id: Optional[str] = None,
    result_url: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            update scene_jobs
            set status = ?, task_id = ?, result_url = ?, error = ?, finished_at = ?
            where id = ?
            """,
            (status, task_id, result_url, error, utc_now(), job_id),
        )


def list_scene_jobs(limit: int = 120) -> List[Dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute(
            """
            select id, scene_id, stage, mode, status, requested_at, finished_at, task_id, result_url, error
            from scene_jobs
            order by requested_at desc
            limit ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def insert_trigger_job(record: Dict[str, Any]) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            insert into trigger_jobs(id, requested_at, mode, provider, status, pid, log_path)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["id"],
                record["requested_at"],
                record["mode"],
                record["provider"],
                record["status"],
                record["pid"],
                record["log_path"],
            ),
        )


def update_trigger_job(job_id: str, *, status: str, exit_code: Optional[int]) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            update trigger_jobs
            set status = ?, exit_code = ?, finished_at = ?
            where id = ?
            """,
            (status, exit_code, utc_now(), job_id),
        )


def list_trigger_jobs(limit: int = 60) -> List[Dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute(
            """
            select id, requested_at, finished_at, mode, provider, status, pid, exit_code, log_path
            from trigger_jobs
            order by requested_at desc
            limit ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def tail_log(path: pathlib.Path, max_lines: int = 180) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])


def reconcile_trigger_jobs() -> None:
    with ACTIVE_TRIGGER_LOCK:
        done_ids: List[str] = []
        for job_id, meta in ACTIVE_TRIGGER_JOBS.items():
            proc: subprocess.Popen[Any] = meta["proc"]
            code = proc.poll()
            if code is None:
                continue
            handle = meta.get("log_handle")
            if handle:
                handle.close()
            status = "completed" if code == 0 else "failed"
            update_trigger_job(job_id, status=status, exit_code=code)
            done_ids.append(job_id)
        for job_id in done_ids:
            ACTIVE_TRIGGER_JOBS.pop(job_id, None)


def summarize_payload(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    run = data.get("run", {}) if isinstance(data.get("run"), dict) else {}
    cloud = data.get("cloud_transfer", {}) if isinstance(data.get("cloud_transfer"), dict) else {}
    scenes = data.get("scenes", [])
    return {
        "run_id": data.get("run_id", ""),
        "story_id": data.get("story_id", ""),
        "status": data.get("status", "unknown"),
        "scene_count": len(scenes) if isinstance(scenes, list) else 0,
        "started_at": run.get("started_at"),
        "ended_at": run.get("ended_at"),
        "cloud_provider": cloud.get("provider"),
        "cloud_status": cloud.get("status"),
        "cloud_destination": cloud.get("destination"),
        "payload_path": str(path.resolve()),
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
    }


def list_runs(limit: int = 140) -> List[Dict[str, Any]]:
    if not RUN_DIR.exists():
        return []
    payloads = sorted(RUN_DIR.glob("payload_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: List[Dict[str, Any]] = []
    for path in payloads[:limit]:
        summary = summarize_payload(path)
        if summary:
            out.append(summary)
    return out


def read_payload_by_run_id(run_id: str) -> Optional[Dict[str, Any]]:
    if not RUN_DIR.exists():
        return None
    for path in RUN_DIR.glob("payload_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("run_id") == run_id:
            return data
    return None


def load_workflow_crons() -> List[str]:
    if not WORKFLOW_PATH.exists():
        return []
    try:
        parsed = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    on_section = parsed.get("on")
    if on_section is None and isinstance(parsed, dict) and True in parsed:
        on_section = parsed.get(True)
    out: List[str] = []
    if isinstance(on_section, dict):
        schedule = on_section.get("schedule", [])
        if isinstance(schedule, list):
            for item in schedule:
                if isinstance(item, dict) and isinstance(item.get("cron"), str):
                    out.append(item["cron"])
    return out


def load_local_cron_entries() -> List[str]:
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False, timeout=5)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    lines = []
    for line in proc.stdout.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if "run_phase5_trigger.py" in text:
            lines.append(text)
    return lines


def is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def maybe_resolve_reference_url(url: str) -> str:
    if "pin.it/" not in url and "pinterest." not in url:
        return url
    if url in REF_CACHE:
        return REF_CACHE[url]
    try:
        response = requests.get(url, timeout=20, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        ctype = response.headers.get("Content-Type", "").lower()
        resolved = response.url
        if "text/html" in ctype:
            patterns = [
                r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            ]
            for pattern in patterns:
                match = re.search(pattern, response.text, flags=re.IGNORECASE)
                if match:
                    resolved = html.unescape(match.group(1))
                    break
        REF_CACHE[url] = resolved
        return resolved
    except Exception:
        return url


def normalize_status(payload: Dict[str, Any]) -> str:
    candidates = [
        payload.get("status"),
        payload.get("state"),
        payload.get("result", {}).get("status") if isinstance(payload.get("result"), dict) else None,
        payload.get("data", {}).get("status") if isinstance(payload.get("data"), dict) else None,
    ]
    for item in candidates:
        if isinstance(item, str) and item:
            return item.strip().lower()
    return "unknown"


def extract_task_id(payload: Dict[str, Any]) -> Optional[str]:
    for key in ("id", "task_id", "prediction_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    if isinstance(payload.get("data"), dict):
        return extract_task_id(payload["data"])
    return None


def collect_urls(value: Any) -> List[str]:
    out: List[str] = []
    if isinstance(value, str):
        if is_url(value):
            out.append(value)
        return out
    if isinstance(value, list):
        for item in value:
            out.extend(collect_urls(item))
        return out
    if isinstance(value, dict):
        for nested in value.values():
            out.extend(collect_urls(nested))
    return out


def extract_error_message(payload: Dict[str, Any]) -> str:
    for key in ("error", "message", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = extract_error_message(value)
            if nested:
                return nested
    if isinstance(payload.get("data"), dict):
        return extract_error_message(payload["data"])
    return "Unknown provider error"


def choose_primary_url(urls: List[str], kind: str) -> str:
    if not urls:
        raise DashboardError(f"No output URL returned for {kind}.")
    lowered = [url.lower() for url in urls]
    priorities = [".png", ".jpg", ".jpeg", ".webp"] if kind == "image" else [".mp4", ".mov", ".webm", ".mkv"]
    for ext in priorities:
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
            raise DashboardError(f"WaveSpeed did not return task id for {model_path}.")
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
                raise DashboardError(f"WaveSpeed task {task_id} failed: {extract_error_message(payload)}")
            if time.time() - started > timeout_sec:
                raise DashboardError(f"WaveSpeed polling timeout for task {task_id}.")
            time.sleep(max(1, poll_interval_sec))

    def upload_local_file(self, path: pathlib.Path) -> str:
        endpoint = f"{WAVESPEED_API_BASE}/media/upload/binary"
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        raw = path.read_bytes()

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
            raise DashboardError(f"Upload returned no URL for {path}.")
        return urls[0]


def get_wavespeed_client() -> WaveSpeedClient:
    api_key = os.getenv("WAVESPEED_API_KEY", "")
    if not api_key:
        raise DashboardError("WAVESPEED_API_KEY is missing.")
    return WaveSpeedClient(api_key=api_key)


def resolve_reference_images(refs: List[str], dry_run: bool, client: Optional[WaveSpeedClient]) -> List[str]:
    out: List[str] = []
    for idx, raw in enumerate(refs):
        if not isinstance(raw, str) or not raw.strip():
            continue
        ref = raw.strip()
        if is_url(ref):
            out.append(maybe_resolve_reference_url(ref))
            continue
        path = pathlib.Path(ref).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        path = path.resolve()
        if not path.exists():
            raise DashboardError(f"Reference image not found: {path}")
        if dry_run:
            out.append(f"https://dry-run.local/ref/{idx}-{path.name}")
            continue
        if client is None:
            raise DashboardError("WaveSpeed client unavailable for local file upload.")
        cache_key = str(path)
        if cache_key in REF_CACHE:
            out.append(REF_CACHE[cache_key])
            continue
        uploaded = client.upload_local_file(path)
        REF_CACHE[cache_key] = uploaded
        out.append(uploaded)

    if not out:
        raise DashboardError("No valid reference images resolved.")
    return out


def get_character_state() -> Dict[str, Any]:
    return {
        "status": get_meta("character_status", "pending"),
        "task_id": get_meta("character_task_id", None),
        "image_url": get_meta("character_image_url", None),
        "last_error": get_meta("character_last_error", None),
        "updated_at": get_meta("character_updated_at", None),
    }


def update_character_state(
    *,
    status: str,
    task_id: Optional[str] = None,
    image_url: Optional[str] = None,
    last_error: Optional[str] = None,
) -> None:
    set_meta("character_status", status)
    set_meta("character_task_id", task_id or "")
    set_meta("character_image_url", image_url or "")
    set_meta("character_last_error", last_error or "")
    set_meta("character_updated_at", utc_now())


def get_style_reference_urls(dry_run: bool, client: Optional[WaveSpeedClient]) -> List[str]:
    config = load_payload_config()
    refs = config.get("style_reference_images", [])
    if not isinstance(refs, list):
        refs = []
    return resolve_reference_images(refs, dry_run=dry_run, client=client)


def generate_character_model(dry_run: bool) -> Dict[str, str]:
    config = load_payload_config()
    generation = get_generation_config()
    character = config.get("character", {}) if isinstance(config.get("character"), dict) else {}
    prompt = str(character.get("character_model_prompt", "")).strip()
    if not prompt:
        raise DashboardError("Character model prompt is missing in payload config.")

    if dry_run:
        task_id = f"dry-character-{uuid.uuid4().hex[:10]}"
        image_url = f"https://dry-run.local/character/{task_id}.png"
        update_character_state(status="completed", task_id=task_id, image_url=image_url, last_error=None)
        return {"task_id": task_id, "image_url": image_url}

    client = get_wavespeed_client()
    style_refs = get_style_reference_urls(dry_run=False, client=client)
    update_character_state(status="running", task_id=None, image_url=None, last_error=None)

    try:
        payload = {
            "prompt": prompt,
            "images": style_refs,
            "resolution": generation["image_resolution"],
            "output_format": generation["image_output_format"],
        }
        submit = client.submit_task(generation["image_model"], payload)
        task_id = extract_task_id(submit) or ""
        result = client.poll_task(task_id, generation["poll_interval_seconds"], generation["poll_timeout_seconds"])
        urls = collect_urls(result.get("output", result))
        image_url = choose_primary_url(urls, kind="image")
        update_character_state(status="completed", task_id=task_id, image_url=image_url, last_error=None)
        return {"task_id": task_id, "image_url": image_url}
    except Exception as exc:  # noqa: BLE001
        update_character_state(status="failed", task_id=None, image_url=None, last_error=str(exc))
        raise


def generate_scene_image(scene_id: str, dry_run: bool) -> Dict[str, str]:
    scene = get_scene(scene_id)
    if scene is None:
        raise DashboardError(f"Scene not found: {scene_id}")

    generation = get_generation_config()
    client = None if dry_run else get_wavespeed_client()

    character = get_character_state()
    character_url = character.get("image_url")
    if not character_url:
        generated = generate_character_model(dry_run=dry_run)
        character_url = generated["image_url"]

    style_refs = get_style_reference_urls(dry_run=dry_run, client=client)
    extra_refs = scene.get("reference_images", [])
    if not isinstance(extra_refs, list):
        extra_refs = []
    resolved_extra = resolve_reference_images(extra_refs, dry_run=dry_run, client=client) if extra_refs else []
    refs = style_refs + [character_url] + resolved_extra

    # de-duplicate while preserving order
    dedup_refs: List[str] = []
    for ref in refs:
        if ref not in dedup_refs:
            dedup_refs.append(ref)

    if dry_run:
        task_id = f"dry-image-{scene_id}-{uuid.uuid4().hex[:8]}"
        image_url = f"https://dry-run.local/scenes/{scene_id}/{task_id}.png"
        return {"task_id": task_id, "url": image_url}

    payload = {
        "prompt": scene["image_prompt"],
        "images": dedup_refs,
        "resolution": generation["image_resolution"],
        "output_format": generation["image_output_format"],
    }
    submit = client.submit_task(generation["image_model"], payload)  # type: ignore[union-attr]
    task_id = extract_task_id(submit) or ""
    result = client.poll_task(task_id, generation["poll_interval_seconds"], generation["poll_timeout_seconds"])  # type: ignore[union-attr]
    urls = collect_urls(result.get("output", result))
    image_url = choose_primary_url(urls, kind="image")
    return {"task_id": task_id, "url": image_url}


def generate_scene_video(scene_id: str, dry_run: bool) -> Dict[str, str]:
    scene = get_scene(scene_id)
    if scene is None:
        raise DashboardError(f"Scene not found: {scene_id}")
    if not scene.get("image_url"):
        raise DashboardError("Scene image missing. Generate image first.")

    generation = get_generation_config()
    if dry_run:
        task_id = f"dry-video-{scene_id}-{uuid.uuid4().hex[:8]}"
        video_url = f"https://dry-run.local/scenes/{scene_id}/{task_id}.mp4"
        return {"task_id": task_id, "url": video_url}

    client = get_wavespeed_client()
    payload = {
        "image": scene["image_url"],
        "prompt": scene["motion_prompt"],
        "duration": generation["video_duration_seconds"],
        "resolution": generation["video_resolution"],
        "movement_amplitude": generation["movement_amplitude"],
        "generate_audio": generation["generate_audio"],
        "bgm": generation["bgm"],
    }
    submit = client.submit_task(generation["video_model"], payload)
    task_id = extract_task_id(submit) or ""
    result = client.poll_task(task_id, generation["poll_interval_seconds"], generation["poll_timeout_seconds"])
    urls = collect_urls(result.get("output", result))
    video_url = choose_primary_url(urls, kind="video")
    return {"task_id": task_id, "url": video_url}


def run_scene_job(job_id: str, scene_id: str, stage: str, dry_run: bool) -> None:
    key = (scene_id, stage)
    try:
        if stage == "image":
            result = generate_scene_image(scene_id, dry_run=dry_run)
            update_scene_fields(
                scene_id,
                {
                    "image_status": "completed",
                    "image_task_id": result["task_id"],
                    "image_url": result["url"],
                    "video_status": "pending",
                    "video_task_id": None,
                    "video_url": None,
                    "last_error": None,
                },
            )
            update_scene_job(
                job_id,
                status="completed",
                task_id=result["task_id"],
                result_url=result["url"],
                error=None,
            )
            return

        if stage == "video":
            result = generate_scene_video(scene_id, dry_run=dry_run)
            update_scene_fields(
                scene_id,
                {
                    "video_status": "completed",
                    "video_task_id": result["task_id"],
                    "video_url": result["url"],
                    "last_error": None,
                },
            )
            update_scene_job(
                job_id,
                status="completed",
                task_id=result["task_id"],
                result_url=result["url"],
                error=None,
            )
            return

        raise DashboardError(f"Unsupported stage: {stage}")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if stage == "image":
            update_scene_fields(scene_id, {"image_status": "failed", "last_error": msg})
        elif stage == "video":
            update_scene_fields(scene_id, {"video_status": "failed", "last_error": msg})
        update_scene_job(job_id, status="failed", error=msg)
    finally:
        with ACTIVE_SCENE_LOCK:
            ACTIVE_SCENE_JOBS.pop(key, None)


def start_scene_job(scene_id: str, stage: str, dry_run: bool) -> Dict[str, Any]:
    scene = get_scene(scene_id)
    if scene is None:
        raise DashboardError(f"Scene not found: {scene_id}")
    if stage not in {"image", "video"}:
        raise DashboardError("stage must be image or video")

    key = (scene_id, stage)
    with ACTIVE_SCENE_LOCK:
        if key in ACTIVE_SCENE_JOBS:
            raise DashboardError(f"{stage} job already running for {scene_id}")

    if stage == "video" and not scene.get("image_url"):
        raise DashboardError("Cannot generate video before image is generated.")

    mode = "dry_run" if dry_run else "live"
    job_id = f"scene-{stage}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    record = {
        "id": job_id,
        "scene_id": scene_id,
        "stage": stage,
        "mode": mode,
        "status": "running",
        "requested_at": utc_now(),
        "task_id": None,
        "result_url": None,
        "error": None,
    }
    insert_scene_job(record)

    if stage == "image":
        update_scene_fields(scene_id, {"image_status": "running", "last_error": None})
    else:
        update_scene_fields(scene_id, {"video_status": "running", "last_error": None})

    thread = threading.Thread(target=run_scene_job, args=(job_id, scene_id, stage, dry_run), daemon=True)
    with ACTIVE_SCENE_LOCK:
        ACTIVE_SCENE_JOBS[key] = {"thread": thread, "job_id": job_id}
    thread.start()
    return record


def start_trigger_job(dry_run: bool, provider: str) -> Dict[str, Any]:
    reconcile_trigger_jobs()
    job_id = f"trigger-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    mode = "dry_run" if dry_run else "live"
    log_path = LOG_DIR / f"dashboard_trigger_{job_id}.log"

    cmd = [
        PYTHON_BIN,
        "tools/run_phase5_trigger.py",
        "--input",
        "tools/config/script_3_hoodrat_payload.json",
        "--out-dir",
        ".tmp/phase5_story3",
        "--provider",
        provider,
    ]
    if dry_run:
        cmd.append("--dry-run")

    env = os.environ.copy()
    log_handle = log_path.open("a", encoding="utf-8")
    proc = subprocess.Popen(  # noqa: S603
        cmd,
        cwd=str(ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        env=env,
    )

    with ACTIVE_TRIGGER_LOCK:
        ACTIVE_TRIGGER_JOBS[job_id] = {
            "proc": proc,
            "log_handle": log_handle,
        }

    job_record = {
        "id": job_id,
        "requested_at": utc_now(),
        "mode": mode,
        "provider": provider,
        "status": "running",
        "pid": proc.pid,
        "log_path": str(log_path.resolve()),
    }
    insert_trigger_job(job_record)
    return job_record


@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.get("/api/overview")
def api_overview() -> Any:
    reconcile_trigger_jobs()
    return jsonify(
        {
            "triggers": {
                "github_workflow": {
                    "name": "phase5-story3-trigger",
                    "workflow_path": str(WORKFLOW_PATH.resolve()),
                    "schedule_cron_utc": load_workflow_crons(),
                },
                "local_webhook": {
                    "listener_script": str((ROOT / "tools" / "webhook_listener.py").resolve()),
                    "path": "/webhook",
                    "health_path": "/health",
                },
                "local_cron": {
                    "installer_script": str((ROOT / "tools" / "install_local_cron.sh").resolve()),
                    "entries": load_local_cron_entries(),
                },
            },
            "script_panel": read_script_panel(),
            "character": get_character_state(),
            "active_trigger_jobs": len(ACTIVE_TRIGGER_JOBS),
            "active_scene_jobs": len(ACTIVE_SCENE_JOBS),
        }
    )


@app.get("/api/scenes")
def api_scenes() -> Any:
    return jsonify({"scenes": list_scenes()})


@app.patch("/api/scenes/<scene_id>")
def api_update_scene(scene_id: str) -> Any:
    payload = request.get_json(silent=True) or {}
    scene = get_scene(scene_id)
    if scene is None:
        return jsonify({"error": "scene not found"}), 404

    updates: Dict[str, Any] = {}
    for field in ("narration", "image_prompt", "motion_prompt"):
        if field in payload and isinstance(payload[field], str):
            updates[field] = payload[field].strip()

    if "reference_images" in payload and isinstance(payload["reference_images"], list):
        updates["reference_images"] = [str(item).strip() for item in payload["reference_images"] if str(item).strip()]

    # Prompt updates invalidate downstream generated artifacts.
    if "image_prompt" in updates and updates["image_prompt"] != scene.get("image_prompt"):
        updates.update(
            {
                "image_status": "pending",
                "image_task_id": None,
                "image_url": None,
                "video_status": "pending",
                "video_task_id": None,
                "video_url": None,
            }
        )
    elif "motion_prompt" in updates and updates["motion_prompt"] != scene.get("motion_prompt"):
        updates.update(
            {
                "video_status": "pending",
                "video_task_id": None,
                "video_url": None,
            }
        )

    updated = update_scene_fields(scene_id, updates)
    return jsonify({"scene": updated})


@app.post("/api/scenes/<scene_id>/generate-image")
def api_generate_scene_image(scene_id: str) -> Any:
    payload = request.get_json(silent=True) or {}
    dry_run = bool(payload.get("dry_run", False))
    try:
        job = start_scene_job(scene_id, stage="image", dry_run=dry_run)
    except DashboardError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(job), 202


@app.post("/api/scenes/<scene_id>/generate-video")
def api_generate_scene_video(scene_id: str) -> Any:
    payload = request.get_json(silent=True) or {}
    dry_run = bool(payload.get("dry_run", False))
    try:
        job = start_scene_job(scene_id, stage="video", dry_run=dry_run)
    except DashboardError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(job), 202


@app.post("/api/scenes/generate-images")
def api_generate_images_batch() -> Any:
    payload = request.get_json(silent=True) or {}
    dry_run = bool(payload.get("dry_run", False))
    only_missing = bool(payload.get("only_missing", True))
    scene_ids_raw = payload.get("scene_ids", [])

    scenes = list_scenes()
    if isinstance(scene_ids_raw, list) and scene_ids_raw:
        wanted = {str(item) for item in scene_ids_raw}
        scenes = [scene for scene in scenes if scene["scene_id"] in wanted]

    launched = []
    errors = []
    for scene in scenes:
        if only_missing and scene.get("image_status") == "completed" and scene.get("image_url"):
            continue
        try:
            launched.append(start_scene_job(scene["scene_id"], stage="image", dry_run=dry_run))
        except Exception as exc:  # noqa: BLE001
            errors.append({"scene_id": scene["scene_id"], "error": str(exc)})

    return jsonify({"launched": launched, "errors": errors}), 202


@app.post("/api/scenes/generate-videos")
def api_generate_videos_batch() -> Any:
    payload = request.get_json(silent=True) or {}
    dry_run = bool(payload.get("dry_run", False))
    only_missing = bool(payload.get("only_missing", True))
    scene_ids_raw = payload.get("scene_ids", [])

    scenes = list_scenes()
    if isinstance(scene_ids_raw, list) and scene_ids_raw:
        wanted = {str(item) for item in scene_ids_raw}
        scenes = [scene for scene in scenes if scene["scene_id"] in wanted]

    launched = []
    errors = []
    for scene in scenes:
        if not scene.get("image_url"):
            continue
        if only_missing and scene.get("video_status") == "completed" and scene.get("video_url"):
            continue
        try:
            launched.append(start_scene_job(scene["scene_id"], stage="video", dry_run=dry_run))
        except Exception as exc:  # noqa: BLE001
            errors.append({"scene_id": scene["scene_id"], "error": str(exc)})

    return jsonify({"launched": launched, "errors": errors}), 202


@app.get("/api/scene-jobs")
def api_scene_jobs() -> Any:
    return jsonify({"jobs": list_scene_jobs()})


@app.get("/api/character")
def api_character() -> Any:
    return jsonify(get_character_state())


@app.post("/api/character/generate")
def api_character_generate() -> Any:
    payload = request.get_json(silent=True) or {}
    dry_run = bool(payload.get("dry_run", False))
    try:
        result = generate_character_model(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    return jsonify(result), 200


@app.get("/api/script")
def api_script_get() -> Any:
    return jsonify(read_script_panel())


@app.patch("/api/script")
def api_script_patch() -> Any:
    payload = request.get_json(silent=True) or {}
    text = payload.get("script_text")
    if not isinstance(text, str):
        return jsonify({"error": "script_text must be string"}), 400
    return jsonify(write_script_text(text))


@app.get("/api/runs")
def api_runs() -> Any:
    reconcile_trigger_jobs()
    return jsonify({"runs": list_runs()})


@app.get("/api/runs/<run_id>")
def api_run_detail(run_id: str) -> Any:
    payload = read_payload_by_run_id(run_id)
    if payload is None:
        return jsonify({"error": "run not found"}), 404
    return jsonify(payload)


@app.get("/api/jobs")
def api_trigger_jobs() -> Any:
    reconcile_trigger_jobs()
    return jsonify({"jobs": list_trigger_jobs()})


@app.get("/api/jobs/<job_id>/log")
def api_trigger_job_log(job_id: str) -> Any:
    reconcile_trigger_jobs()
    jobs = {job["id"]: job for job in list_trigger_jobs(limit=200)}
    job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    log_path = pathlib.Path(job["log_path"])
    return jsonify({"job_id": job_id, "log": tail_log(log_path)})


@app.post("/api/trigger")
def api_trigger() -> Any:
    payload = request.get_json(silent=True) or {}
    dry_run = bool(payload.get("dry_run", False))
    provider = str(payload.get("provider", "auto")).strip().lower()
    if provider not in {"auto", "supabase", "cloudinary"}:
        return jsonify({"error": "provider must be auto, supabase, or cloudinary"}), 400
    try:
        job = start_trigger_job(dry_run=dry_run, provider=provider)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    return jsonify(job), 202


def main() -> None:
    load_env_file(ROOT / ".env")
    init_db()
    sync_scenes_from_payload()
    app.run(host="127.0.0.1", port=5055, debug=False)


if __name__ == "__main__":
    main()
