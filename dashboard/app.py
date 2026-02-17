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
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import requests
import yaml
from flask import Flask, Response, jsonify, render_template, request, stream_with_context


ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA_ROOT_RAW = os.environ.get("DASHBOARD_DATA_ROOT", str(ROOT))
DATA_ROOT = pathlib.Path(DATA_ROOT_RAW).expanduser()
if not DATA_ROOT.is_absolute():
    DATA_ROOT = (ROOT / DATA_ROOT).resolve()

TMP_DIR = DATA_ROOT / ".tmp"
RUN_DIR = TMP_DIR / "phase5_story3"
LOG_DIR = TMP_DIR / "logs"
DASH_DIR = TMP_DIR / "dashboard"
DB_PATH = DASH_DIR / "dashboard.db"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "phase5_story3_trigger.yml"
PAYLOAD_CONFIG_PATH = ROOT / "tools" / "config" / "script_3_hoodrat_payload.json"
DEFAULT_SCRIPT_PATH = ROOT / "tools" / "config" / "script_3_voiceover.md"

PYTHON_BIN = os.environ.get("PYTHON", "python3")
WAVESPEED_API_BASE = "https://api.wavespeed.ai/api/v3"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKIMEDIA_COMMONS_API = "https://commons.wikimedia.org/w/api.php"
DUCKDUCKGO_LITE_SEARCH = "https://lite.duckduckgo.com/lite/"
AUDIT_HTTP_HEADERS = {"User-Agent": "APRT-Broller/1.0 (+dashboard)"}
SUCCESS_STATUSES = {"succeeded", "completed", "success"}
FAIL_STATUSES = {"failed", "error", "canceled", "cancelled"}

ACTIVE_TRIGGER_JOBS: Dict[str, Dict[str, Any]] = {}
ACTIVE_TRIGGER_LOCK = threading.Lock()
ACTIVE_SCENE_JOBS: Dict[Tuple[str, str], Dict[str, Any]] = {}
ACTIVE_SCENE_LOCK = threading.Lock()
REF_CACHE: Dict[str, str] = {}
STYLE_DESC_CACHE: Dict[str, str] = {}
BOOTSTRAPPED = False
BOOTSTRAP_LOCK = threading.Lock()

app = Flask(__name__, template_folder="templates", static_folder="static")

DEFAULT_STYLE_DESCRIPTION = (
    "anime-inspired cel-shaded style, cinematic framing, expressive linework, clean gradients, "
    "bold silhouettes, and high-contrast lighting"
)
STYLE_IP_GUARDRAIL = (
    "Depict only an original character design. Do not depict any recognizable copyrighted franchise anime or manga character."
)
REFERENCE_USAGE_GUARDRAIL = (
    "Use reference images for style only (palette, texture, line weight, lighting, composition), "
    "never for copying characters."
)
SERVERLESS_ENV_KEYS = ("VERCEL", "NOW_REGION", "AWS_REGION")


class DashboardError(RuntimeError):
    """Raised for user-visible failures."""


def is_serverless_runtime() -> bool:
    for key in SERVERLESS_ENV_KEYS:
        value = os.getenv(key, "")
        if isinstance(value, str) and value.strip():
            return True
    return False


def is_simulated_url(url: str) -> bool:
    return isinstance(url, str) and url.startswith("https://dry-run.local/")


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

            create table if not exists character_registry (
                id text primary key,
                name text not null,
                name_key text not null,
                aliases text not null default '[]',
                image_url text not null,
                source_url text,
                source_label text,
                audit_score real not null default 0,
                audit_status text not null default 'verified',
                audit_log text not null default '{}',
                created_at text not null,
                updated_at text not null,
                last_used_at text not null
            );

            create table if not exists character_audit_events (
                id text primary key,
                requested_at text not null,
                story_id text,
                target_name text not null,
                status text not null,
                score real,
                selected_image_url text,
                selected_source_url text,
                details text not null
            );
            """
        )
        conn.execute(
            "create index if not exists idx_character_registry_name_key on character_registry(name_key)"
        )
        conn.execute(
            "create index if not exists idx_character_audit_events_target on character_audit_events(target_name)"
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


def payload_override_path() -> pathlib.Path:
    return DASH_DIR / "payload_config_override.json"


def load_payload_base_config() -> Dict[str, Any]:
    if not PAYLOAD_CONFIG_PATH.exists():
        raise DashboardError(f"Missing payload config: {PAYLOAD_CONFIG_PATH}")
    return json.loads(PAYLOAD_CONFIG_PATH.read_text(encoding="utf-8"))


def load_payload_override() -> Dict[str, Any]:
    path = payload_override_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def deep_merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = json.loads(json.dumps(base))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_payload_config() -> Dict[str, Any]:
    base = load_payload_base_config()
    override = load_payload_override()
    if not override:
        return base
    return deep_merge_dict(base, override)


def save_payload_config(config: Dict[str, Any]) -> str:
    try:
        PAYLOAD_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        PAYLOAD_CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        return str(PAYLOAD_CONFIG_PATH.resolve())
    except OSError:
        path = payload_override_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        return str(path.resolve())


def contains_case_insensitive(text: str, phrase: str) -> bool:
    if not phrase.strip():
        return True
    return re.search(re.escape(phrase.strip()), text, flags=re.IGNORECASE) is not None


def sanitize_style_description(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return DEFAULT_STYLE_DESCRIPTION
    banned_patterns = [
        r"\bgoku\b",
        r"\bdragon\s*ball\b",
        r"\bdragonball\b",
        r"\bvegeta\b",
        r"\bgohan\b",
        r"\bnaruto\b",
        r"\bluffy\b",
        r"\bsasuke\b",
        r"\bitachi\b",
    ]
    for pattern in banned_patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" ,.;")
    return text or DEFAULT_STYLE_DESCRIPTION


def get_style_description_from_config(config: Dict[str, Any]) -> str:
    raw = config.get("style_description")
    if isinstance(raw, str) and raw.strip():
        return sanitize_style_description(raw)
    refs = config.get("style_reference_images", [])
    inferred = infer_style_description_from_refs(refs if isinstance(refs, list) else [])
    if inferred:
        return sanitize_style_description(inferred)
    return sanitize_style_description(DEFAULT_STYLE_DESCRIPTION)


def style_guardrail_text(style_description: str) -> str:
    safe_style = sanitize_style_description(style_description)
    return (
        f"Style direction: {safe_style}. "
        f"{REFERENCE_USAGE_GUARDRAIL} {STYLE_IP_GUARDRAIL}"
    ).strip()


def should_use_style_reference_images(config: Dict[str, Any]) -> bool:
    generation = config.get("generation", {})
    if isinstance(generation, dict) and isinstance(generation.get("use_style_reference_images"), bool):
        return bool(generation.get("use_style_reference_images"))
    return False


def infer_style_description_from_refs(refs: List[str]) -> str:
    for ref in refs:
        if not isinstance(ref, str) or not ref.strip():
            continue
        inferred = infer_style_description_from_ref(ref.strip())
        if inferred:
            return inferred
    return ""


def infer_style_description_from_ref(ref: str) -> str:
    if ref in STYLE_DESC_CACHE:
        return STYLE_DESC_CACHE[ref]
    try:
        response = requests.get(ref, timeout=20, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        text = response.text.lower() if "text/html" in (response.headers.get("Content-Type", "").lower()) else ref.lower()
        if re.search(r"\banime\b|\bmanga\b|\bcel[- ]?shad", text):
            STYLE_DESC_CACHE[ref] = DEFAULT_STYLE_DESCRIPTION
            return STYLE_DESC_CACHE[ref]
        if re.search(r"\bcartoon\b|\billustration\b", text):
            STYLE_DESC_CACHE[ref] = (
                "stylized anime-cartoon blend, cel-shaded rendering, bold outlines, cinematic color grading"
            )
            return STYLE_DESC_CACHE[ref]
    except Exception:
        pass
    STYLE_DESC_CACHE[ref] = ""
    return ""


def strip_legacy_style_clauses(text: str) -> str:
    out = text
    patterns = [
        r"Visual style lock:[^.]+(?:\.[^.]+)*\.",
        r"Not anime\.",
        r"Not Goku\.",
        r"No Goku\.",
        r"No Dragon Ball characters\.",
        r"No anime aura or spiky anime hair\.",
        r"Do not copy any copyrighted anime character or specific reference subject\.",
    ]
    for pattern in patterns:
        out = re.sub(pattern, " ", out, flags=re.IGNORECASE)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def ensure_character_in_prompt(prompt: str, character_name: str, kind: str) -> str:
    base = strip_legacy_style_clauses(prompt.strip())
    if not base:
        base = "Story beat visual prompt."
    if character_name.strip() and not contains_case_insensitive(base, character_name):
        if kind == "motion":
            base = f"{base} Keep {character_name} clearly visible and on-model throughout the motion."
        else:
            base = f"{base} Feature {character_name} as the primary on-screen character."
    return base.strip()


def normalize_scene_prompt_with_guardrails(prompt: str, character_name: str, kind: str, style_description: str) -> str:
    base = ensure_character_in_prompt(prompt, character_name, kind)
    guard = style_guardrail_text(style_description)
    if not contains_case_insensitive(base, STYLE_IP_GUARDRAIL):
        base = f"{base} {guard}"
    return base.strip()


def enforce_scene_prompts_with_character_name(
    character_name: str, style_description: str, persist_payload: bool = True
) -> Dict[str, int]:
    character_name = character_name.strip()
    config = load_payload_config()
    scenes = config.get("scenes", [])
    if not isinstance(scenes, list):
        return {"payload_updates": 0, "db_updates": 0}

    payload_updates = 0
    db_updates = 0
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        scene_id = str(scene.get("scene_id", "")).strip()
        image_before = str(scene.get("image_prompt", "")).strip()
        motion_before = str(scene.get("motion_prompt", "")).strip()
        image_after = normalize_scene_prompt_with_guardrails(image_before, character_name, "image", style_description)
        motion_after = normalize_scene_prompt_with_guardrails(motion_before, character_name, "motion", style_description)

        if image_after != image_before or motion_after != motion_before:
            scene["image_prompt"] = image_after
            scene["motion_prompt"] = motion_after
            payload_updates += 1

        if scene_id:
            row = get_scene(scene_id)
            if row is not None and (row.get("image_prompt") != image_after or row.get("motion_prompt") != motion_after):
                update_scene_fields(scene_id, {"image_prompt": image_after, "motion_prompt": motion_after})
                db_updates += 1

    if payload_updates > 0 and persist_payload:
        save_payload_config(config)
    return {"payload_updates": payload_updates, "db_updates": db_updates}


def get_character_config() -> Dict[str, Any]:
    config = load_payload_config()
    character = config.get("character", {}) if isinstance(config.get("character"), dict) else {}
    identity_cfg = get_character_identity_config(config)
    refs = config.get("style_reference_images", [])
    if not isinstance(refs, list):
        refs = []
    style_description = get_style_description_from_config(config)
    name = str(character.get("name", "")).strip()
    model_prompt = str(character.get("character_model_prompt", "")).strip()
    notes = str(character.get("consistency_notes", "")).strip()
    effective_prompt = build_character_prompt(
        name=name,
        model_prompt=model_prompt,
        consistency_notes=notes,
        style_description=style_description,
    )
    inferred_target = infer_story_target_character_name()
    registry_match = get_character_registry_record_by_name(inferred_target) if inferred_target else None
    return {
        "name": name,
        "character_model_prompt": model_prompt,
        "consistency_notes": notes,
        "style_description": style_description,
        "use_style_reference_images": should_use_style_reference_images(config),
        "style_reference_images": [str(item).strip() for item in refs if str(item).strip()],
        "character_identity": identity_cfg,
        "style_guardrail": style_guardrail_text(style_description),
        "effective_prompt": effective_prompt,
        "inferred_target_name": inferred_target,
        "registry_match": {
            "id": registry_match.get("id"),
            "name": registry_match.get("name"),
            "image_url": registry_match.get("image_url"),
            "audit_score": registry_match.get("audit_score"),
        }
        if isinstance(registry_match, dict)
        else None,
    }


def build_character_prompt(*, name: str, model_prompt: str, consistency_notes: str, style_description: str) -> str:
    parts: List[str] = []
    raw_prompt = strip_legacy_style_clauses(model_prompt.strip())
    if raw_prompt:
        parts.append(raw_prompt)
    if name.strip() and not contains_case_insensitive(raw_prompt, name):
        parts.append(f"Character name: {name}.")
    has_style_guard = contains_case_insensitive(raw_prompt, STYLE_IP_GUARDRAIL) and contains_case_insensitive(
        raw_prompt, REFERENCE_USAGE_GUARDRAIL
    )
    if not has_style_guard:
        parts.append(style_guardrail_text(style_description))
    if consistency_notes.strip():
        parts.append(f"Consistency requirements: {consistency_notes.strip()}")
    return " ".join(parts).strip()


def parse_style_reference_images(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        lines = [line.strip() for line in raw.replace(",", "\n").splitlines()]
        return [line for line in lines if line]
    return []


def update_character_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    config = load_payload_config()
    character = config.get("character", {}) if isinstance(config.get("character"), dict) else {}
    identity_cfg = get_character_identity_config(config)

    if "name" in payload:
        character["name"] = str(payload.get("name", "")).strip()
    if "character_model_prompt" in payload:
        character["character_model_prompt"] = str(payload.get("character_model_prompt", "")).strip()
    if "consistency_notes" in payload:
        character["consistency_notes"] = str(payload.get("consistency_notes", "")).strip()

    config["character"] = character

    if "style_description" in payload:
        config["style_description"] = sanitize_style_description(str(payload.get("style_description", "")).strip())
    if "use_style_reference_images" in payload:
        generation = config.get("generation", {}) if isinstance(config.get("generation"), dict) else {}
        generation["use_style_reference_images"] = bool(payload.get("use_style_reference_images"))
        config["generation"] = generation

    if "style_reference_images" in payload:
        refs = parse_style_reference_images(payload.get("style_reference_images"))
        config["style_reference_images"] = refs

    if "character_identity" in payload and isinstance(payload.get("character_identity"), dict):
        incoming = payload.get("character_identity", {})
        if "audit_enabled" in incoming:
            identity_cfg["audit_enabled"] = bool(incoming.get("audit_enabled"))
        if "auto_reuse_saved_model" in incoming:
            identity_cfg["auto_reuse_saved_model"] = bool(incoming.get("auto_reuse_saved_model"))
        if "min_confidence_score" in incoming:
            try:
                identity_cfg["min_confidence_score"] = max(0.2, min(0.95, float(incoming.get("min_confidence_score"))))
            except Exception:
                pass
        if "sources" in incoming and isinstance(incoming.get("sources"), list):
            identity_cfg["sources"] = [
                str(item).strip().lower()
                for item in incoming.get("sources", [])
                if str(item).strip().lower() in {"duckduckgo_web", "duckduckgo", "wikipedia", "wikimedia_commons"}
            ]
            identity_cfg["sources"] = [
                "duckduckgo_web" if source == "duckduckgo" else source for source in identity_cfg["sources"]
            ]
            identity_cfg["sources"] = list(dict.fromkeys(identity_cfg["sources"])) or identity_cfg["sources"]

    if "audit_enabled" in payload:
        identity_cfg["audit_enabled"] = bool(payload.get("audit_enabled"))
    if "auto_reuse_saved_model" in payload:
        identity_cfg["auto_reuse_saved_model"] = bool(payload.get("auto_reuse_saved_model"))
    if "min_confidence_score" in payload:
        try:
            identity_cfg["min_confidence_score"] = max(0.2, min(0.95, float(payload.get("min_confidence_score"))))
        except Exception:
            pass
    if "audit_sources" in payload and isinstance(payload.get("audit_sources"), list):
        cleaned_sources: List[str] = []
        for item in payload.get("audit_sources", []):
            source = str(item).strip().lower()
            if source == "duckduckgo":
                source = "duckduckgo_web"
            if source in {"duckduckgo_web", "wikipedia", "wikimedia_commons"} and source not in cleaned_sources:
                cleaned_sources.append(source)
        if cleaned_sources:
            identity_cfg["sources"] = cleaned_sources

    if not isinstance(identity_cfg.get("sources"), list) or not identity_cfg.get("sources"):
        identity_cfg["sources"] = ["duckduckgo_web", "wikipedia", "wikimedia_commons"]

    config["character_identity"] = identity_cfg

    saved_path = save_payload_config(config)
    sync_scenes_from_payload()
    character_name = str(character.get("name", "")).strip()
    style_description = get_style_description_from_config(config)
    normalized = (
        enforce_scene_prompts_with_character_name(character_name, style_description, persist_payload=True)
        if character_name
        else {"payload_updates": 0, "db_updates": 0}
    )
    return {
        "saved_path": saved_path,
        "character_config": get_character_config(),
        "normalized_prompts": normalized,
    }


def get_generation_config() -> Dict[str, Any]:
    cfg = load_payload_config()
    generation = cfg.get("generation") if isinstance(cfg.get("generation"), dict) else {}
    image_model = generation.get("image_model", "google/nano-banana-pro/edit")
    video_model = generation.get("video_model", "wavespeed-ai/wan-2.2/image-to-video")
    raw_duration = int(generation.get("video_duration_seconds", 5))
    video_duration = raw_duration
    # WaveSpeed WAN 2.2 currently accepts duration values of 5 or 8 seconds.
    if "wan-2.2/image-to-video" in str(video_model).lower() and raw_duration not in {5, 8}:
        video_duration = 5
    return {
        "image_model": image_model,
        "video_model": video_model,
        "image_resolution": generation.get("image_resolution", "1k"),
        "image_output_format": generation.get("image_output_format", "png"),
        "video_resolution": generation.get("video_resolution", "720p"),
        "video_duration_seconds": video_duration,
        "movement_amplitude": generation.get("movement_amplitude", "auto"),
        "generate_audio": bool(generation.get("generate_audio", True)),
        "bgm": bool(generation.get("bgm", True)),
        "use_style_reference_images": bool(generation.get("use_style_reference_images", False)),
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
    override_path = DASH_DIR / "script_override.md"
    if override_path.exists():
        return {
            "script_path": str(override_path.resolve()),
            "script_text": override_path.read_text(encoding="utf-8"),
        }

    script_path = discover_script_path()
    text = script_path.read_text(encoding="utf-8") if script_path.exists() else ""
    return {
        "script_path": str(script_path),
        "script_text": text,
    }


def write_script_text(new_text: str) -> Dict[str, Any]:
    script_path = discover_script_path()
    try:
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(new_text, encoding="utf-8")
        return read_script_panel()
    except OSError:
        override_path = DASH_DIR / "script_override.md"
        override_path.parent.mkdir(parents=True, exist_ok=True)
        override_path.write_text(new_text, encoding="utf-8")
        return {
            "script_path": str(override_path.resolve()),
            "script_text": new_text,
        }


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


def bootstrap_once() -> None:
    global BOOTSTRAPPED, DATA_ROOT, TMP_DIR, RUN_DIR, LOG_DIR, DASH_DIR, DB_PATH
    if BOOTSTRAPPED:
        return
    with BOOTSTRAP_LOCK:
        if BOOTSTRAPPED:
            return
        load_env_file(ROOT / ".env")
        data_root_raw = os.environ.get("DASHBOARD_DATA_ROOT", str(ROOT))
        data_root = pathlib.Path(data_root_raw).expanduser()
        if not data_root.is_absolute():
            data_root = (ROOT / data_root).resolve()

        DATA_ROOT = data_root
        TMP_DIR = DATA_ROOT / ".tmp"
        RUN_DIR = TMP_DIR / "phase5_story3"
        LOG_DIR = TMP_DIR / "logs"
        DASH_DIR = TMP_DIR / "dashboard"
        DB_PATH = DASH_DIR / "dashboard.db"

        ensure_dirs()
        init_db()
        sync_scenes_from_payload()
        cfg = load_payload_config()
        character = cfg.get("character", {}) if isinstance(cfg.get("character"), dict) else {}
        character_name = str(character.get("name", "")).strip()
        if not character_name:
            character_name = ensure_story_character_name_persisted()
            cfg = load_payload_config()
        style_description = get_style_description_from_config(cfg)
        if character_name:
            enforce_scene_prompts_with_character_name(character_name, style_description, persist_payload=False)
            backfill_registry_from_current_character_state()
        BOOTSTRAPPED = True


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


def set_scene_job_task_id(job_id: str, task_id: str) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            update scene_jobs
            set task_id = ?
            where id = ?
            """,
            (task_id, job_id),
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


def get_scene_job(job_id: str) -> Optional[Dict[str, Any]]:
    with db_conn() as conn:
        row = conn.execute(
            """
            select id, scene_id, stage, mode, status, requested_at, finished_at, task_id, result_url, error
            from scene_jobs
            where id = ?
            """,
            (job_id,),
        ).fetchone()
    return dict(row) if row is not None else None


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


def reconcile_provider_jobs() -> None:
    refresh_character_state_from_provider()
    refresh_running_scene_jobs_from_provider()


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


def normalize_name_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def parse_json_or_default(raw: Any, fallback: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def get_character_identity_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = config if isinstance(config, dict) else load_payload_config()
    raw = cfg.get("character_identity", {}) if isinstance(cfg.get("character_identity"), dict) else {}
    sources_raw = raw.get("sources", ["duckduckgo_web", "wikipedia", "wikimedia_commons"])
    if not isinstance(sources_raw, list):
        sources_raw = ["duckduckgo_web", "wikipedia", "wikimedia_commons"]

    allowed_sources = {"duckduckgo_web", "wikipedia", "wikimedia_commons"}
    normalized_sources: List[str] = []
    for item in sources_raw:
        source = str(item or "").strip().lower()
        if source == "duckduckgo":
            source = "duckduckgo_web"
        if source in allowed_sources and source not in normalized_sources:
            normalized_sources.append(source)
    if not normalized_sources:
        normalized_sources = ["duckduckgo_web", "wikipedia", "wikimedia_commons"]

    raw_min = raw.get("min_confidence_score", 0.6)
    try:
        min_score = float(raw_min)
    except Exception:
        min_score = 0.6
    min_score = max(0.2, min(0.95, min_score))

    return {
        "audit_enabled": bool(raw.get("audit_enabled", True)),
        "auto_reuse_saved_model": bool(raw.get("auto_reuse_saved_model", True)),
        "min_confidence_score": min_score,
        "sources": normalized_sources,
    }


def collect_story_context() -> Dict[str, Any]:
    cfg = load_payload_config()
    character = cfg.get("character", {}) if isinstance(cfg.get("character"), dict) else {}
    name = str(character.get("name", "")).strip()
    title = str(cfg.get("title", "")).strip()
    story_id = str(cfg.get("story_id", "")).strip()
    scenes = cfg.get("scenes", [])
    narrations: List[str] = []
    if isinstance(scenes, list):
        narrations = [str(item.get("narration", "")).strip() for item in scenes if isinstance(item, dict)]
    script = read_script_panel().get("script_text", "")
    parts = [title, name, story_id, script] + narrations
    merged_text = "\n".join([part for part in parts if isinstance(part, str) and part.strip()])
    return {
        "story_id": story_id,
        "title": title,
        "character_name": name,
        "script_text": script,
        "narrations": narrations,
        "merged_text": merged_text,
    }


def extract_story_person_candidates(text: str, max_names: int = 10) -> List[str]:
    if not text.strip():
        return []
    candidates: List[str] = []
    seen: set[str] = set()

    def add_candidate(raw: str) -> None:
        cleaned = re.sub(r"\s+", " ", raw).strip(" .,:;!?\"'()[]{}")
        if not cleaned:
            return
        words = cleaned.split()
        if len(words) < 2 or len(words) > 4:
            return
        if not all(re.match(r"^[A-Z][a-zA-Z'-]+$", word) for word in words):
            return
        banned = {
            "The Internet",
            "Good Morning",
            "Palm Beach",
            "Script Panel",
            "Trigger Dashboard",
        }
        if cleaned in banned:
            return
        key = normalize_name_key(cleaned)
        if not key or key in seen:
            return
        seen.add(key)
        candidates.append(cleaned)

    for match in re.findall(r"\bnamed\s+([A-Z][a-zA-Z'-]+(?:\s+[A-Z][a-zA-Z'-]+){1,3})", text):
        add_candidate(match)
    for match in re.findall(r"\b([A-Z][a-zA-Z'-]+(?:\s+[A-Z][a-zA-Z'-]+){1,3})\b", text):
        add_candidate(match)
        if len(candidates) >= max_names:
            break
    return candidates[:max_names]


def infer_story_target_character_name() -> str:
    context = collect_story_context()
    existing = str(context.get("character_name", "")).strip()
    if existing:
        return existing
    candidates = extract_story_person_candidates(str(context.get("merged_text", "")))
    return candidates[0] if candidates else ""


def set_character_audit_state(state: Dict[str, Any]) -> None:
    payload = json.dumps(state, ensure_ascii=True)
    set_meta("character_audit_state", payload)


def get_character_audit_state() -> Dict[str, Any]:
    raw = get_meta("character_audit_state", "")
    parsed = parse_json_or_default(raw, {})
    return parsed if isinstance(parsed, dict) else {}


def list_character_registry(limit: int = 60) -> List[Dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute(
            """
            select id, name, name_key, aliases, image_url, source_url, source_label,
                   audit_score, audit_status, audit_log, created_at, updated_at, last_used_at
            from character_registry
            order by last_used_at desc, updated_at desc
            limit ?
            """,
            (limit,),
        ).fetchall()
    items: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["aliases"] = parse_json_or_default(item.get("aliases", "[]"), [])
        item["audit_log"] = parse_json_or_default(item.get("audit_log", "{}"), {})
        items.append(item)
    return items


def count_character_registry() -> int:
    with db_conn() as conn:
        row = conn.execute("select count(1) as n from character_registry").fetchone()
    return int(row["n"]) if row is not None else 0


def get_character_registry_record_by_name(name: str) -> Optional[Dict[str, Any]]:
    name = str(name or "").strip()
    if not name:
        return None
    name_key = normalize_name_key(name)
    with db_conn() as conn:
        row = conn.execute(
            """
            select id, name, name_key, aliases, image_url, source_url, source_label,
                   audit_score, audit_status, audit_log, created_at, updated_at, last_used_at
            from character_registry
            where name_key = ?
            order by updated_at desc
            limit 1
            """,
            (name_key,),
        ).fetchone()
    if row is not None:
        record = dict(row)
        record["aliases"] = parse_json_or_default(record.get("aliases", "[]"), [])
        record["audit_log"] = parse_json_or_default(record.get("audit_log", "{}"), {})
        return record

    # alias fallback
    for record in list_character_registry(limit=200):
        aliases = record.get("aliases", [])
        if not isinstance(aliases, list):
            continue
        alias_keys = {normalize_name_key(str(alias)) for alias in aliases}
        if name_key in alias_keys:
            return record
    return None


def upsert_character_registry_record(
    *,
    name: str,
    image_url: str,
    source_url: Optional[str],
    source_label: Optional[str],
    audit_score: float,
    audit_status: str,
    audit_log: Dict[str, Any],
) -> Dict[str, Any]:
    now = utc_now()
    existing = get_character_registry_record_by_name(name)
    aliases_list: List[str] = []
    if existing is not None:
        existing_aliases = existing.get("aliases", [])
        if isinstance(existing_aliases, list):
            aliases_list = [str(item).strip() for item in existing_aliases if str(item).strip()]
    if name not in aliases_list:
        aliases_list.append(name)

    if existing is None:
        record_id = f"chr-{uuid.uuid4().hex[:12]}"
        with db_conn() as conn:
            conn.execute(
                """
                insert into character_registry(
                    id, name, name_key, aliases, image_url, source_url, source_label,
                    audit_score, audit_status, audit_log, created_at, updated_at, last_used_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    name,
                    normalize_name_key(name),
                    json.dumps(aliases_list, ensure_ascii=True),
                    image_url,
                    source_url or "",
                    source_label or "",
                    float(audit_score),
                    audit_status,
                    json.dumps(audit_log, ensure_ascii=True),
                    now,
                    now,
                    now,
                ),
            )
    else:
        record_id = existing["id"]
        with db_conn() as conn:
            conn.execute(
                """
                update character_registry
                set name = ?, aliases = ?, image_url = ?, source_url = ?, source_label = ?,
                    audit_score = ?, audit_status = ?, audit_log = ?, updated_at = ?, last_used_at = ?
                where id = ?
                """,
                (
                    name,
                    json.dumps(aliases_list, ensure_ascii=True),
                    image_url,
                    source_url or "",
                    source_label or "",
                    float(audit_score),
                    audit_status,
                    json.dumps(audit_log, ensure_ascii=True),
                    now,
                    now,
                    record_id,
                ),
            )
    record = get_character_registry_record_by_name(name)
    if record is None:
        raise DashboardError("Character registry upsert failed.")
    return record


def mark_character_registry_used(record_id: str) -> None:
    with db_conn() as conn:
        conn.execute(
            "update character_registry set last_used_at = ?, updated_at = ? where id = ?",
            (utc_now(), utc_now(), record_id),
        )


def insert_character_audit_event(
    *,
    story_id: str,
    target_name: str,
    status: str,
    score: float,
    selected_image_url: str,
    selected_source_url: str,
    details: Dict[str, Any],
) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            insert into character_audit_events(
                id, requested_at, story_id, target_name, status, score,
                selected_image_url, selected_source_url, details
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"audit-{uuid.uuid4().hex[:12]}",
                utc_now(),
                story_id,
                target_name,
                status,
                float(score),
                selected_image_url,
                selected_source_url,
                json.dumps(details, ensure_ascii=True),
            ),
        )


def strip_html_tags(text: str) -> str:
    raw = str(text or "")
    raw = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.IGNORECASE | re.DOTALL)
    raw = re.sub(r"<style[^>]*>.*?</style>", " ", raw, flags=re.IGNORECASE | re.DOTALL)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    return re.sub(r"\s+", " ", raw).strip()


def compact_text(text: str, max_len: int = 260) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    return cleaned[:max_len]


def decode_duckduckgo_redirect(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    host = (parsed.netloc or "").lower()
    if "duckduckgo.com" not in host:
        return value
    query = parse_qs(parsed.query)
    uddg = query.get("uddg", [])
    if uddg:
        return unquote(uddg[0])
    return value


def extract_meta_image_url(html_text: str) -> str:
    patterns = [
        r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, flags=re.IGNORECASE)
        if match:
            return html.unescape(match.group(1).strip())
    return ""


def score_candidate_identity(
    *,
    target_name: str,
    candidate_text: str,
    image_url: str,
    source_bias: float,
) -> Tuple[int, float]:
    tokens = [token.lower() for token in re.findall(r"[A-Za-z0-9]+", target_name) if len(token) >= 2]
    haystack = str(candidate_text or "").lower()
    matched_tokens = sum(
        1
        for token in tokens
        if re.search(rf"\b{re.escape(token)}\b", haystack) is not None
    )
    coverage = matched_tokens / max(1, len(tokens))
    score = coverage + float(source_bias)
    if normalize_name_key(target_name) and normalize_name_key(target_name) in normalize_name_key(candidate_text):
        score += 0.22
    if str(image_url or "").strip():
        score += 0.12

    lowered = haystack.lower()
    if any(flag in lowered for flag in ("disambiguation", "list of", "category:", "fictional character")):
        score -= 0.25
    if any(flag in lowered for flag in ("dragon ball", "goku", "naruto", "anime character")):
        score -= 0.18
    if "logo" in lowered:
        score -= 0.12

    return matched_tokens, max(0.0, min(1.0, round(score, 3)))


def search_wikipedia_candidates(target_name: str) -> List[Dict[str, Any]]:
    search_response = requests.get(
        WIKIPEDIA_API,
        params={
            "action": "query",
            "list": "search",
            "srsearch": target_name,
            "srlimit": 8,
            "format": "json",
            "utf8": 1,
        },
        timeout=20,
        headers=AUDIT_HTTP_HEADERS,
    )
    search_response.raise_for_status()
    search_data = search_response.json()
    search_items = search_data.get("query", {}).get("search", [])
    titles = [str(item.get("title", "")).strip() for item in search_items if isinstance(item, dict)]
    titles = [title for title in titles if title]

    out: List[Dict[str, Any]] = []
    for title in titles[:6]:
        detail_response = requests.get(
            WIKIPEDIA_API,
            params={
                "action": "query",
                "prop": "pageimages|extracts|info",
                "titles": title,
                "pithumbsize": 1200,
                "exintro": 1,
                "explaintext": 1,
                "inprop": "url",
                "format": "json",
                "utf8": 1,
            },
            timeout=20,
            headers=AUDIT_HTTP_HEADERS,
        )
        detail_response.raise_for_status()
        pages = detail_response.json().get("query", {}).get("pages", {})
        if not isinstance(pages, dict):
            continue
        for page in pages.values():
            if not isinstance(page, dict):
                continue
            page_title = str(page.get("title", title)).strip()
            extract = str(page.get("extract", "")).strip()
            fullurl = str(page.get("fullurl", "")).strip()
            thumb = page.get("thumbnail", {}) if isinstance(page.get("thumbnail"), dict) else {}
            image_url = str(thumb.get("source", "")).strip()
            matched_tokens, score = score_candidate_identity(
                target_name=target_name,
                candidate_text=f"{page_title} {extract}",
                image_url=image_url,
                source_bias=0.1,
            )
            out.append(
                {
                    "source": "wikipedia",
                    "title": page_title,
                    "image_url": image_url,
                    "source_url": fullurl,
                    "summary": compact_text(extract),
                    "matched_tokens": matched_tokens,
                    "score": score,
                }
            )
    return out


def search_wikimedia_commons_candidates(target_name: str) -> List[Dict[str, Any]]:
    response = requests.get(
        WIKIMEDIA_COMMONS_API,
        params={
            "action": "query",
            "generator": "search",
            "gsrsearch": target_name,
            "gsrnamespace": 6,
            "gsrlimit": 10,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata",
            "iiurlwidth": 1400,
            "format": "json",
            "utf8": 1,
        },
        timeout=20,
        headers=AUDIT_HTTP_HEADERS,
    )
    response.raise_for_status()
    pages = response.json().get("query", {}).get("pages", {})
    if not isinstance(pages, dict):
        return []

    out: List[Dict[str, Any]] = []
    for page in pages.values():
        if not isinstance(page, dict):
            continue
        title = str(page.get("title", "")).strip()
        infos = page.get("imageinfo", [])
        if not isinstance(infos, list) or not infos:
            continue
        info = infos[0] if isinstance(infos[0], dict) else {}
        image_url = str(info.get("thumburl") or info.get("url") or "").strip()
        source_url = str(info.get("descriptionurl") or "").strip()
        ext = info.get("extmetadata", {}) if isinstance(info.get("extmetadata"), dict) else {}
        desc_html = (
            ext.get("ImageDescription", {}).get("value", "")
            if isinstance(ext.get("ImageDescription"), dict)
            else ""
        )
        description = compact_text(strip_html_tags(str(desc_html)))
        matched_tokens, score = score_candidate_identity(
            target_name=target_name,
            candidate_text=f"{title} {description}",
            image_url=image_url,
            source_bias=0.08,
        )
        out.append(
            {
                "source": "wikimedia_commons",
                "title": title,
                "image_url": image_url,
                "source_url": source_url,
                "summary": description,
                "matched_tokens": matched_tokens,
                "score": score,
            }
        )
    return out


def search_duckduckgo_candidates(target_name: str) -> List[Dict[str, Any]]:
    response = requests.get(
        DUCKDUCKGO_LITE_SEARCH,
        params={"q": target_name},
        timeout=20,
        headers=AUDIT_HTTP_HEADERS,
    )
    response.raise_for_status()
    body = response.text
    pattern = re.compile(
        r"<a rel=\"nofollow\" href=\"([^\"]+)\" class='result-link'>(.*?)</a>",
        flags=re.IGNORECASE | re.DOTALL,
    )

    out: List[Dict[str, Any]] = []
    for match in pattern.finditer(body):
        if len(out) >= 8:
            break
        href = html.unescape(match.group(1))
        title_html = match.group(2)
        title = compact_text(strip_html_tags(title_html), max_len=160)
        window = body[match.end() : match.end() + 2500]
        snippet_match = re.search(r"<td class='result-snippet'>(.*?)</td>", window, flags=re.IGNORECASE | re.DOTALL)
        snippet = compact_text(strip_html_tags(snippet_match.group(1)) if snippet_match else "", max_len=220)
        resolved_url = decode_duckduckgo_redirect(href)
        if not is_url(resolved_url):
            continue

        page_title = ""
        page_image_url = ""
        try:
            page_response = requests.get(resolved_url, timeout=16, headers=AUDIT_HTTP_HEADERS, allow_redirects=True)
            ctype = str(page_response.headers.get("Content-Type", "")).lower()
            if "text/html" in ctype:
                page_html = page_response.text[:300000]
                page_title_match = re.search(r"<title[^>]*>(.*?)</title>", page_html, flags=re.IGNORECASE | re.DOTALL)
                if page_title_match:
                    page_title = compact_text(strip_html_tags(page_title_match.group(1)), max_len=160)
                page_image_url = extract_meta_image_url(page_html)
        except Exception:
            page_title = ""
            page_image_url = ""

        matched_tokens, score = score_candidate_identity(
            target_name=target_name,
            candidate_text=f"{title} {snippet} {page_title}",
            image_url=page_image_url,
            source_bias=0.12,
        )
        out.append(
            {
                "source": "duckduckgo_web",
                "title": title,
                "image_url": page_image_url,
                "source_url": resolved_url,
                "summary": snippet,
                "matched_tokens": matched_tokens,
                "score": score,
            }
        )
    return out


def dedupe_audit_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    dedup: Dict[str, Dict[str, Any]] = {}
    for item in candidates:
        title = str(item.get("title", "")).strip()
        source_url = str(item.get("source_url", "")).strip()
        image_url = str(item.get("image_url", "")).strip()
        source = str(item.get("source", "")).strip()
        key = "|".join(
            [
                normalize_name_key(title),
                normalize_name_key(source_url),
                normalize_name_key(image_url),
                normalize_name_key(source),
            ]
        )
        current = dedup.get(key)
        if current is None or float(item.get("score", 0)) > float(current.get("score", 0)):
            dedup[key] = item
    return sorted(dedup.values(), key=lambda item: float(item.get("score", 0)), reverse=True)


def run_character_identity_audit(
    target_name: str,
    min_score: float = 0.6,
    sources: Optional[List[str]] = None,
) -> Dict[str, Any]:
    target_name = str(target_name or "").strip()
    if not target_name:
        raise DashboardError("Character audit requires a target name.")

    cfg = load_payload_config()
    identity_cfg = get_character_identity_config(cfg)
    selected_sources = (
        [
            "duckduckgo_web" if str(source).strip().lower() == "duckduckgo" else str(source).strip().lower()
            for source in (sources or identity_cfg.get("sources", []))
            if str(source).strip()
        ]
        or ["duckduckgo_web", "wikipedia", "wikimedia_commons"]
    )
    selected_sources = [
        source for source in selected_sources if source in {"duckduckgo_web", "wikipedia", "wikimedia_commons"}
    ]
    selected_sources = list(dict.fromkeys(selected_sources))
    threshold = max(0.2, min(0.95, float(min_score)))

    candidates: List[Dict[str, Any]] = []
    source_errors: List[str] = []

    for source in selected_sources:
        try:
            if source == "duckduckgo_web":
                candidates.extend(search_duckduckgo_candidates(target_name))
            elif source == "wikipedia":
                candidates.extend(search_wikipedia_candidates(target_name))
            elif source == "wikimedia_commons":
                candidates.extend(search_wikimedia_commons_candidates(target_name))
        except Exception as exc:  # noqa: BLE001
            source_errors.append(f"{source}: {exc}")

    ranked = dedupe_audit_candidates(candidates)
    verified = [
        item
        for item in ranked
        if str(item.get("image_url", "")).strip() and float(item.get("score", 0)) >= threshold
    ]
    review_threshold = max(0.35, threshold * 0.72)
    review = [
        item
        for item in ranked
        if str(item.get("image_url", "")).strip() and float(item.get("score", 0)) >= review_threshold
    ]
    best_score = float(ranked[0].get("score", 0)) if ranked else 0.0

    status = "failed"
    if verified:
        status = "verified"
    elif ranked:
        status = "needs_review"

    selected_images = [str(item.get("image_url")) for item in verified[:3]]
    selected_sources_urls = [str(item.get("source_url")) for item in verified[:3] if str(item.get("source_url", "")).strip()]
    review_images = [str(item.get("image_url")) for item in review[:3]]
    review_sources_urls = [str(item.get("source_url")) for item in review[:3] if str(item.get("source_url", "")).strip()]

    context = collect_story_context()
    result = {
        "target_name": target_name,
        "status": status,
        "score": best_score,
        "sources_used": selected_sources,
        "source_errors": source_errors,
        "selected_reference_images": selected_images,
        "selected_source_urls": selected_sources_urls,
        "review_reference_images": review_images,
        "review_source_urls": review_sources_urls,
        "candidates": ranked[:12],
        "requested_at": utc_now(),
        "story_id": str(context.get("story_id", "")).strip(),
    }

    existing_record = get_character_registry_record_by_name(target_name)
    if existing_record is not None and str(existing_record.get("image_url", "")).strip():
        upsert_character_registry_record(
            name=target_name,
            image_url=str(existing_record.get("image_url", "")),
            source_url=str(existing_record.get("source_url", "")).strip() or (selected_sources_urls[0] if selected_sources_urls else ""),
            source_label=str(existing_record.get("source_label", "")).strip() or "registry_existing",
            audit_score=best_score,
            audit_status=status,
            audit_log=result,
        )

    set_character_audit_state(result)
    insert_character_audit_event(
        story_id=result["story_id"],
        target_name=target_name,
        status=status,
        score=best_score,
        selected_image_url=selected_images[0] if selected_images else "",
        selected_source_url=selected_sources_urls[0] if selected_sources_urls else "",
        details=result,
    )
    return result


def auto_bind_character_from_registry(force: bool = False) -> Dict[str, Any]:
    if not force:
        cfg = load_payload_config()
        identity_cfg = get_character_identity_config(cfg)
        if not bool(identity_cfg.get("auto_reuse_saved_model", True)):
            return {"bound": False, "reason": "auto_reuse_disabled"}

    state = get_character_state()
    current_status = str(state.get("status") or "").lower()
    if current_status == "running" and not force:
        return {"bound": False, "reason": "character_generation_running"}

    target_name = infer_story_target_character_name()
    if not target_name:
        return {"bound": False, "reason": "no_story_character_detected"}

    record = get_character_registry_record_by_name(target_name)
    if record is None:
        return {"bound": False, "reason": "no_registry_match", "target_name": target_name}

    existing_image = str(state.get("image_url") or "").strip()
    if existing_image and not force and existing_image != str(record.get("image_url", "")).strip():
        return {"bound": False, "reason": "character_image_already_set", "target_name": target_name}

    task_id = f"registry-{record['id']}"
    update_character_state(
        status="completed",
        task_id=task_id,
        image_url=str(record.get("image_url", "")),
        last_error=None,
    )
    mark_character_registry_used(record["id"])
    audit_state = {
        "target_name": target_name,
        "status": "reused",
        "score": float(record.get("audit_score", 0)),
        "selected_reference_images": [str(record.get("image_url", ""))],
        "selected_source_urls": [str(record.get("source_url", ""))] if str(record.get("source_url", "")).strip() else [],
        "candidates": [],
        "requested_at": utc_now(),
        "story_id": str(load_payload_config().get("story_id", "")).strip(),
        "registry_id": record["id"],
    }
    set_character_audit_state(audit_state)
    return {
        "bound": True,
        "target_name": target_name,
        "registry_record": record,
    }


def save_character_to_registry_from_state(image_url: str, source: str) -> Optional[Dict[str, Any]]:
    if not str(image_url or "").strip():
        return None
    target_name = infer_story_target_character_name()
    if not target_name:
        return None
    audit_state = get_character_audit_state()
    selected_sources = audit_state.get("selected_source_urls", [])
    source_url = ""
    if isinstance(selected_sources, list) and selected_sources:
        source_url = str(selected_sources[0])
    if source == "registry_reuse":
        existing = get_character_registry_record_by_name(target_name)
        if existing is not None:
            mark_character_registry_used(existing["id"])
            return existing

    record = upsert_character_registry_record(
        name=target_name,
        image_url=str(image_url).strip(),
        source_url=source_url,
        source_label=source,
        audit_score=float(audit_state.get("score", 0) or 0),
        audit_status=str(audit_state.get("status", "verified") or "verified"),
        audit_log=audit_state if isinstance(audit_state, dict) else {},
    )
    return record


def backfill_registry_from_current_character_state() -> Optional[Dict[str, Any]]:
    state = get_character_state()
    image_url = str(state.get("image_url") or "").strip()
    if not image_url or is_simulated_url(image_url):
        return None
    target_name = infer_story_target_character_name()
    if not target_name:
        return None
    existing = get_character_registry_record_by_name(target_name)
    if existing is not None:
        return existing
    return save_character_to_registry_from_state(image_url=image_url, source="backfill_existing_state")


def ensure_story_character_name_persisted() -> str:
    config = load_payload_config()
    character = config.get("character", {}) if isinstance(config.get("character"), dict) else {}
    current_name = str(character.get("name", "")).strip()
    if current_name:
        return current_name
    inferred = infer_story_target_character_name()
    if not inferred:
        return ""
    character["name"] = inferred
    config["character"] = character
    save_payload_config(config)
    style_description = get_style_description_from_config(config)
    enforce_scene_prompts_with_character_name(inferred, style_description, persist_payload=True)
    return inferred


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


def latest_payload_path() -> Optional[pathlib.Path]:
    if not RUN_DIR.exists():
        return None
    payloads = sorted(RUN_DIR.glob("payload_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return payloads[0] if payloads else None


def stream_file_response(path: pathlib.Path, download_name: str, mimetype: str = "application/octet-stream") -> Response:
    def generate() -> Iterable[bytes]:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(64 * 1024)
                if not chunk:
                    break
                yield chunk

    headers = {"Content-Disposition": f'attachment; filename="{download_name}"'}
    return Response(stream_with_context(generate()), headers=headers, mimetype=mimetype)


def stream_remote_url_response(url: str, download_name: str) -> Response:
    response = requests.get(url, stream=True, timeout=90)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type") or mimetypes.guess_type(url)[0] or "application/octet-stream"
    headers = {"Content-Disposition": f'attachment; filename="{download_name}"'}

    def generate() -> Iterable[bytes]:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if chunk:
                yield chunk

    return Response(stream_with_context(generate()), headers=headers, mimetype=content_type)


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
        wrapped_body = {
            "enable_base64_output": False,
            "input": input_payload,
        }
        response = requests.post(endpoint, headers=self._headers(), json=wrapped_body, timeout=self.timeout_sec)

        # Some models/endpoints expect prompt/image fields at top level instead of under "input".
        if not response.ok and response.status_code == 400:
            fallback_body: Dict[str, Any] = {"enable_base64_output": False}
            fallback_body.update(input_payload)
            fallback = requests.post(endpoint, headers=self._headers(), json=fallback_body, timeout=self.timeout_sec)
            if fallback.ok:
                response = fallback
            else:
                detail = fallback.text.strip() or response.text.strip()
                if len(detail) > 700:
                    detail = detail[:700] + "..."
                raise DashboardError(f"WaveSpeed submit failed ({fallback.status_code}): {detail}")

        if not response.ok:
            detail = response.text.strip()
            if len(detail) > 700:
                detail = detail[:700] + "..."
            raise DashboardError(f"WaveSpeed submit failed ({response.status_code}): {detail}")

        payload = response.json()
        task_id = extract_task_id(payload)
        if not task_id:
            raise DashboardError(f"WaveSpeed did not return task id for {model_path}.")
        return payload

    def get_task(self, task_id: str) -> Dict[str, Any]:
        endpoints = [
            f"{WAVESPEED_API_BASE}/predictions/{task_id}/result",
            f"{WAVESPEED_API_BASE}/predictions/{task_id}",
        ]
        last_404 = False
        for endpoint in endpoints:
            response = requests.get(endpoint, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=self.timeout_sec)
            if response.status_code == 404:
                last_404 = True
                continue
            if not response.ok:
                detail = response.text.strip()
                if len(detail) > 700:
                    detail = detail[:700] + "..."
                raise DashboardError(f"WaveSpeed task lookup failed ({response.status_code}): {detail}")
            try:
                return response.json()
            except ValueError as exc:
                raise DashboardError(f"WaveSpeed task lookup returned non-JSON for task {task_id}.") from exc
        if last_404:
            raise DashboardError(f"WaveSpeed task lookup failed for {task_id}: endpoint not found.")
        raise DashboardError(f"WaveSpeed task lookup failed for {task_id}.")

    def poll_task(self, task_id: str, poll_interval_sec: int, timeout_sec: int) -> Dict[str, Any]:
        started = time.time()
        while True:
            payload = self.get_task(task_id)
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
    task_id = get_meta("character_task_id", None)
    task_text = str(task_id or "").strip()
    source = "generated"
    registry_id = ""
    if task_text.startswith("registry-"):
        source = "registry_reuse"
        registry_id = task_text.replace("registry-", "", 1)
    elif not task_text:
        source = "pending"
    return {
        "status": get_meta("character_status", "pending"),
        "task_id": task_id,
        "image_url": get_meta("character_image_url", None),
        "last_error": get_meta("character_last_error", None),
        "updated_at": get_meta("character_updated_at", None),
        "source": source,
        "registry_id": registry_id or None,
        "audit": get_character_audit_state(),
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


def refresh_character_state_from_provider() -> None:
    state = get_character_state()
    if (state.get("status") or "").lower() != "running":
        return
    task_id = str(state.get("task_id") or "").strip()
    if not task_id:
        return
    api_key = os.getenv("WAVESPEED_API_KEY", "").strip()
    if not api_key:
        return
    try:
        payload = WaveSpeedClient(api_key=api_key, timeout_sec=15).get_task(task_id)
        status = normalize_status(payload)
        if status in SUCCESS_STATUSES:
            urls = collect_urls(payload.get("output", payload))
            image_url = choose_primary_url(urls, kind="image")
            update_character_state(status="completed", task_id=task_id, image_url=image_url, last_error=None)
            save_character_to_registry_from_state(image_url=image_url, source="generated")
        elif status in FAIL_STATUSES:
            update_character_state(
                status="failed",
                task_id=task_id,
                image_url=None,
                last_error=extract_error_message(payload),
            )
    except Exception:
        return


def refresh_running_scene_jobs_from_provider(max_jobs: int = 10) -> None:
    api_key = os.getenv("WAVESPEED_API_KEY", "").strip()
    if not api_key:
        return
    with db_conn() as conn:
        rows = conn.execute(
            """
            select id, scene_id, stage, task_id
            from scene_jobs
            where status = 'running' and task_id is not null and task_id != ''
            order by requested_at asc
            limit ?
            """,
            (max_jobs,),
        ).fetchall()
    if not rows:
        return

    client = WaveSpeedClient(api_key=api_key, timeout_sec=15)
    for row in rows:
        job_id = row["id"]
        scene_id = row["scene_id"]
        stage = row["stage"]
        task_id = row["task_id"]
        if not scene_id or not stage or not task_id:
            continue
        try:
            payload = client.get_task(task_id)
        except Exception:
            continue
        status = normalize_status(payload)
        if status in SUCCESS_STATUSES:
            try:
                urls = collect_urls(payload.get("output", payload))
                url = choose_primary_url(urls, kind=stage)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if stage == "image":
                    update_scene_fields(scene_id, {"image_status": "failed", "last_error": msg})
                else:
                    update_scene_fields(scene_id, {"video_status": "failed", "last_error": msg})
                update_scene_job(job_id, status="failed", task_id=task_id, error=msg)
                continue

            if stage == "image":
                update_scene_fields(
                    scene_id,
                    {
                        "image_status": "completed",
                        "image_task_id": task_id,
                        "image_url": url,
                        "video_status": "pending",
                        "video_task_id": None,
                        "video_url": None,
                        "last_error": None,
                    },
                )
            else:
                update_scene_fields(
                    scene_id,
                    {
                        "video_status": "completed",
                        "video_task_id": task_id,
                        "video_url": url,
                        "last_error": None,
                    },
                )
            update_scene_job(job_id, status="completed", task_id=task_id, result_url=url, error=None)
            continue

        if status in FAIL_STATUSES:
            msg = extract_error_message(payload)
            if stage == "image":
                update_scene_fields(scene_id, {"image_status": "failed", "last_error": msg})
            else:
                update_scene_fields(scene_id, {"video_status": "failed", "last_error": msg})
            update_scene_job(job_id, status="failed", task_id=task_id, error=msg)


def get_style_reference_urls(dry_run: bool, client: Optional[WaveSpeedClient]) -> List[str]:
    config = load_payload_config()
    refs = config.get("style_reference_images", [])
    if not isinstance(refs, list):
        refs = []
    if not refs:
        return []
    return resolve_reference_images(refs, dry_run=dry_run, client=client)


def preflight_character_for_scene_images(*, dry_run: bool) -> None:
    if dry_run:
        return
    auto_bind_character_from_registry(force=False)
    refresh_character_state_from_provider()
    state = get_character_state()
    image_url = str(state.get("image_url") or "").strip()
    if image_url:
        return

    status = str(state.get("status") or "").strip().lower()
    task_id = str(state.get("task_id") or "").strip()
    last_error = str(state.get("last_error") or "").strip()

    if status == "failed":
        suffix = f": {last_error}" if last_error else ""
        raise DashboardError(f"Character model generation failed{suffix}")
    if status == "running":
        suffix = f" (task {task_id})" if task_id else ""
        raise DashboardError(f"Character model is still generating{suffix}. Wait until it completes, then retry scene image.")

    if is_serverless_runtime():
        generated = generate_character_model(dry_run=False, submit_only=True)
        new_task = generated.get("task_id", "")
        suffix = f" (task {new_task})" if new_task else ""
        raise DashboardError(
            f"Character model generation started{suffix}. Wait until status is completed, then retry scene image."
        )


def generate_character_model(dry_run: bool, submit_only: bool = False) -> Dict[str, str]:
    ensured_name = ensure_story_character_name_persisted()
    config = load_payload_config()
    generation = get_generation_config()
    character = config.get("character", {}) if isinstance(config.get("character"), dict) else {}
    name = str(character.get("name", "")).strip() or ensured_name
    model_prompt = str(character.get("character_model_prompt", "")).strip()
    consistency_notes = str(character.get("consistency_notes", "")).strip()
    style_description = get_style_description_from_config(config)

    # If this story already has a saved character model, reuse it instantly.
    reuse = auto_bind_character_from_registry(force=False)
    if reuse.get("bound"):
        record = reuse.get("registry_record", {}) if isinstance(reuse.get("registry_record"), dict) else {}
        return {
            "task_id": f"registry-{record.get('id', '')}",
            "image_url": str(record.get("image_url", "")),
        }

    identity_cfg = get_character_identity_config(config)
    audit_enabled = bool(identity_cfg.get("audit_enabled", True))
    min_score = float(identity_cfg.get("min_confidence_score", 0.6) or 0.6)
    audit_sources = identity_cfg.get("sources", [])

    discovered_refs: List[str] = []
    if audit_enabled and name:
        try:
            audit = run_character_identity_audit(name, min_score=min_score, sources=audit_sources)
            selected = audit.get("selected_reference_images", [])
            if not isinstance(selected, list) or not selected:
                selected = audit.get("review_reference_images", [])
            if isinstance(selected, list):
                discovered_refs = [str(item).strip() for item in selected if str(item).strip()]
        except Exception as exc:  # noqa: BLE001
            set_character_audit_state(
                {
                    "target_name": name,
                    "status": "failed",
                    "score": 0,
                    "selected_reference_images": [],
                    "selected_source_urls": [],
                    "candidates": [],
                    "requested_at": utc_now(),
                    "story_id": str(config.get("story_id", "")).strip(),
                    "error": str(exc),
                }
            )

    style_ref_list = config.get("style_reference_images", [])
    style_ref_list = style_ref_list if isinstance(style_ref_list, list) else []
    merged_ref_list: List[str] = []
    for ref in discovered_refs + [str(item).strip() for item in style_ref_list if str(item).strip()]:
        if ref and ref not in merged_ref_list:
            merged_ref_list.append(ref)
    if merged_ref_list != style_ref_list:
        config["style_reference_images"] = merged_ref_list
        save_payload_config(config)

    prompt = build_character_prompt(
        name=name,
        model_prompt=model_prompt,
        consistency_notes=consistency_notes,
        style_description=style_description,
    )
    if not prompt:
        raise DashboardError("Character model prompt is missing in payload config.")

    if dry_run:
        task_id = f"dry-character-{uuid.uuid4().hex[:10]}"
        image_url = f"https://dry-run.local/character/{task_id}.png"
        update_character_state(status="completed", task_id=task_id, image_url=image_url, last_error=None)
        return {"task_id": task_id, "image_url": image_url}

    client = get_wavespeed_client()
    style_refs = get_style_reference_urls(dry_run=False, client=client)
    if not style_refs:
        raise DashboardError("At least one style reference image is required for character generation.")
    update_character_state(status="running", task_id=None, image_url=None, last_error=None)

    try:
        payload: Dict[str, Any] = {
            "prompt": prompt,
            "images": style_refs,
            "resolution": generation["image_resolution"],
            "output_format": generation["image_output_format"],
        }
        submit = client.submit_task(generation["image_model"], payload)
        task_id = extract_task_id(submit) or ""
        if submit_only:
            update_character_state(status="running", task_id=task_id, image_url=None, last_error=None)
            return {"task_id": task_id, "image_url": ""}
        result = client.poll_task(task_id, generation["poll_interval_seconds"], generation["poll_timeout_seconds"])
        urls = collect_urls(result.get("output", result))
        image_url = choose_primary_url(urls, kind="image")
        update_character_state(status="completed", task_id=task_id, image_url=image_url, last_error=None)
        save_character_to_registry_from_state(image_url=image_url, source="generated")
        return {"task_id": task_id, "image_url": image_url}
    except Exception as exc:  # noqa: BLE001
        update_character_state(status="failed", task_id=None, image_url=None, last_error=str(exc))
        raise


def generate_scene_image(scene_id: str, dry_run: bool, submit_only: bool = False) -> Dict[str, str]:
    scene = get_scene(scene_id)
    if scene is None:
        raise DashboardError(f"Scene not found: {scene_id}")

    generation = get_generation_config()
    config = load_payload_config()
    character = config.get("character", {}) if isinstance(config.get("character"), dict) else {}
    character_name = str(character.get("name", "")).strip()
    style_description = get_style_description_from_config(config)
    client = None if dry_run else get_wavespeed_client()

    if not dry_run:
        refresh_character_state_from_provider()
    character_state = get_character_state()
    character_url = character_state.get("image_url")
    if not character_url:
        if dry_run:
            generated = generate_character_model(dry_run=True, submit_only=False)
            character_url = generated.get("image_url")
        else:
            status = str(character_state.get("status") or "").strip().lower()
            task_id = str(character_state.get("task_id") or "").strip()
            last_error = str(character_state.get("last_error") or "").strip()
            if status == "failed":
                suffix = f": {last_error}" if last_error else ""
                raise DashboardError(f"Character model generation failed{suffix}")
            if status == "running":
                suffix = f" (task {task_id})" if task_id else ""
                raise DashboardError(
                    f"Character model is still generating{suffix}. Wait until it completes, then retry scene image."
                )
            if is_serverless_runtime():
                generated = generate_character_model(dry_run=False, submit_only=True)
                new_task = generated.get("task_id", "")
                suffix = f" (task {new_task})" if new_task else ""
                raise DashboardError(
                    f"Character model generation started{suffix}. Wait until status is completed, then retry scene image."
                )
            generated = generate_character_model(dry_run=False, submit_only=False)
            character_url = generated.get("image_url")
    if not character_url:
        raise DashboardError("Character model image URL missing. Generate character model first.")

    style_refs: List[str] = []
    if generation["use_style_reference_images"]:
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

    effective_prompt = normalize_scene_prompt_with_guardrails(
        str(scene.get("image_prompt", "")),
        character_name=character_name,
        kind="image",
        style_description=style_description,
    )

    payload = {
        "prompt": effective_prompt,
        "images": dedup_refs,
        "resolution": generation["image_resolution"],
        "output_format": generation["image_output_format"],
    }
    submit = client.submit_task(generation["image_model"], payload)  # type: ignore[union-attr]
    task_id = extract_task_id(submit) or ""
    if submit_only:
        return {"task_id": task_id, "url": ""}
    result = client.poll_task(task_id, generation["poll_interval_seconds"], generation["poll_timeout_seconds"])  # type: ignore[union-attr]
    urls = collect_urls(result.get("output", result))
    image_url = choose_primary_url(urls, kind="image")
    return {"task_id": task_id, "url": image_url}


def generate_scene_video(scene_id: str, dry_run: bool, submit_only: bool = False) -> Dict[str, str]:
    scene = get_scene(scene_id)
    if scene is None:
        raise DashboardError(f"Scene not found: {scene_id}")
    if not scene.get("image_url"):
        raise DashboardError("Scene image missing. Generate image first.")
    if not dry_run and is_simulated_url(str(scene.get("image_url", ""))):
        raise DashboardError("Scene image is from dry-run simulation. Generate a live image first.")

    generation = get_generation_config()
    config = load_payload_config()
    character = config.get("character", {}) if isinstance(config.get("character"), dict) else {}
    character_name = str(character.get("name", "")).strip()
    style_description = get_style_description_from_config(config)
    if dry_run:
        task_id = f"dry-video-{scene_id}-{uuid.uuid4().hex[:8]}"
        video_url = f"https://dry-run.local/scenes/{scene_id}/{task_id}.mp4"
        return {"task_id": task_id, "url": video_url}

    client = get_wavespeed_client()
    effective_prompt = normalize_scene_prompt_with_guardrails(
        str(scene.get("motion_prompt", "")),
        character_name=character_name,
        kind="motion",
        style_description=style_description,
    )
    payload = {
        "image": scene["image_url"],
        "prompt": effective_prompt,
        "duration": generation["video_duration_seconds"],
        "resolution": generation["video_resolution"],
        "movement_amplitude": generation["movement_amplitude"],
        "generate_audio": generation["generate_audio"],
        "bgm": generation["bgm"],
    }
    submit = client.submit_task(generation["video_model"], payload)
    task_id = extract_task_id(submit) or ""
    if submit_only:
        return {"task_id": task_id, "url": ""}
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
    if is_serverless_runtime():
        with db_conn() as conn:
            row = conn.execute(
                """
                select id from scene_jobs
                where scene_id = ? and stage = ? and status = 'running'
                order by requested_at desc
                limit 1
                """,
                (scene_id, stage),
            ).fetchone()
        if row is not None:
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

    # Serverless runtimes are request-scoped, so background threads do not persist reliably.
    # Dry-run executes inline; live mode submits to provider and is reconciled on refresh.
    if is_serverless_runtime():
        if not dry_run:
            try:
                if stage == "image":
                    result = generate_scene_image(scene_id, dry_run=False, submit_only=True)
                    update_scene_fields(scene_id, {"image_status": "running", "image_task_id": result["task_id"], "last_error": None})
                else:
                    result = generate_scene_video(scene_id, dry_run=False, submit_only=True)
                    update_scene_fields(scene_id, {"video_status": "running", "video_task_id": result["task_id"], "last_error": None})
                set_scene_job_task_id(job_id, result["task_id"])
                latest = get_scene_job(job_id)
                return latest or record
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if stage == "image":
                    update_scene_fields(scene_id, {"image_status": "failed", "last_error": msg})
                else:
                    update_scene_fields(scene_id, {"video_status": "failed", "last_error": msg})
                update_scene_job(job_id, status="failed", error=msg)
                raise DashboardError(msg)

        run_scene_job(job_id, scene_id, stage, dry_run=True)
        latest = get_scene_job(job_id)
        return latest or record

    thread = threading.Thread(target=run_scene_job, args=(job_id, scene_id, stage, dry_run), daemon=True)
    with ACTIVE_SCENE_LOCK:
        ACTIVE_SCENE_JOBS[key] = {"thread": thread, "job_id": job_id}
    thread.start()
    return record


def start_trigger_job(dry_run: bool, provider: str) -> Dict[str, Any]:
    reconcile_trigger_jobs()
    job_id = f"trigger-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    requested_at = utc_now()
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

    if is_serverless_runtime():
        raise DashboardError(
            "Run Full Trigger is disabled on Vercel serverless (read-only filesystem). "
            "Use scene-level buttons here, or run full trigger from local CLI/GitHub workflow."
        )

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
        "requested_at": requested_at,
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
    bootstrap_once()
    return render_template("index.html")


@app.get("/favicon.ico")
def favicon() -> Any:
    bootstrap_once()
    return Response(status=204)


@app.get("/api/overview")
def api_overview() -> Any:
    bootstrap_once()
    reconcile_trigger_jobs()
    reconcile_provider_jobs()
    backfill_registry_from_current_character_state()
    try:
        auto_bind_character_from_registry(force=False)
    except Exception:
        pass
    registry_items = list_character_registry(limit=8)
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
            "character_audit_state": get_character_audit_state(),
            "character_registry": {"count": count_character_registry(), "items": registry_items},
            "runtime": {
                "serverless": is_serverless_runtime(),
                "wavespeed_configured": bool(os.getenv("WAVESPEED_API_KEY", "").strip()),
            },
            "active_trigger_jobs": len(ACTIVE_TRIGGER_JOBS),
            "active_scene_jobs": len(ACTIVE_SCENE_JOBS),
        }
    )


@app.get("/api/scenes")
def api_scenes() -> Any:
    bootstrap_once()
    reconcile_provider_jobs()
    return jsonify({"scenes": list_scenes()})


@app.patch("/api/scenes/<scene_id>")
def api_update_scene(scene_id: str) -> Any:
    bootstrap_once()
    payload = request.get_json(silent=True) or {}
    scene = get_scene(scene_id)
    if scene is None:
        return jsonify({"error": "scene not found"}), 404

    character_cfg = get_character_config()
    character_name = str(character_cfg.get("name", "")).strip()
    style_description = str(character_cfg.get("style_description", DEFAULT_STYLE_DESCRIPTION)).strip()
    updates: Dict[str, Any] = {}
    for field in ("narration", "image_prompt", "motion_prompt"):
        if field in payload and isinstance(payload[field], str):
            updates[field] = payload[field].strip()

    if "image_prompt" in updates:
        updates["image_prompt"] = normalize_scene_prompt_with_guardrails(
            updates["image_prompt"], character_name, "image", style_description
        )
    if "motion_prompt" in updates:
        updates["motion_prompt"] = normalize_scene_prompt_with_guardrails(
            updates["motion_prompt"], character_name, "motion", style_description
        )

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
    bootstrap_once()
    payload = request.get_json(silent=True) or {}
    dry_run = bool(payload.get("dry_run", False))
    try:
        preflight_character_for_scene_images(dry_run=dry_run)
        job = start_scene_job(scene_id, stage="image", dry_run=dry_run)
    except DashboardError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(job), 202


@app.post("/api/scenes/<scene_id>/generate-video")
def api_generate_scene_video(scene_id: str) -> Any:
    bootstrap_once()
    payload = request.get_json(silent=True) or {}
    dry_run = bool(payload.get("dry_run", False))
    try:
        job = start_scene_job(scene_id, stage="video", dry_run=dry_run)
    except DashboardError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(job), 202


@app.post("/api/scenes/generate-images")
def api_generate_images_batch() -> Any:
    bootstrap_once()
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
    try:
        preflight_character_for_scene_images(dry_run=dry_run)
    except DashboardError as exc:
        return jsonify({"launched": [], "errors": [{"scene_id": "__character__", "error": str(exc)}]}), 400
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
    bootstrap_once()
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
    bootstrap_once()
    reconcile_provider_jobs()
    return jsonify({"jobs": list_scene_jobs()})


@app.get("/api/character")
def api_character() -> Any:
    bootstrap_once()
    reconcile_provider_jobs()
    backfill_registry_from_current_character_state()
    try:
        auto_bind_character_from_registry(force=False)
    except Exception:
        pass
    return jsonify(get_character_state())


@app.get("/api/character/config")
def api_character_config_get() -> Any:
    bootstrap_once()
    return jsonify(get_character_config())


@app.patch("/api/character/config")
def api_character_config_patch() -> Any:
    bootstrap_once()
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid JSON body"}), 400
    try:
        result = update_character_config(payload)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.post("/api/character/generate")
def api_character_generate() -> Any:
    bootstrap_once()
    payload = request.get_json(silent=True) or {}
    dry_run = bool(payload.get("dry_run", False))
    try:
        submit_only = is_serverless_runtime() and not dry_run
        result = generate_character_model(dry_run=dry_run, submit_only=submit_only)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    return jsonify(result), 200


@app.get("/api/character/audit")
def api_character_audit_get() -> Any:
    bootstrap_once()
    identity_cfg = get_character_identity_config()
    return jsonify(
        {
            "audit": get_character_audit_state(),
            "target_name": infer_story_target_character_name(),
            "registry_count": count_character_registry(),
            "character_identity": identity_cfg,
        }
    )


@app.post("/api/character/audit")
def api_character_audit_post() -> Any:
    bootstrap_once()
    payload = request.get_json(silent=True) or {}
    target_name = str(payload.get("target_name", "")).strip() or infer_story_target_character_name()
    if not target_name:
        return jsonify({"error": "No character name found in current story/script."}), 400
    try:
        score = float(payload.get("min_confidence_score", 0.6) or 0.6)
        sources = payload.get("sources")
        sources_list = sources if isinstance(sources, list) else None
        result = run_character_identity_audit(target_name, min_score=score, sources=sources_list)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.get("/api/character/registry")
def api_character_registry_get() -> Any:
    bootstrap_once()
    return jsonify({"count": count_character_registry(), "items": list_character_registry(limit=120)})


@app.post("/api/character/auto-bind")
def api_character_auto_bind() -> Any:
    bootstrap_once()
    payload = request.get_json(silent=True) or {}
    force = bool(payload.get("force", False))
    try:
        result = auto_bind_character_from_registry(force=force)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.get("/api/script")
def api_script_get() -> Any:
    bootstrap_once()
    return jsonify(read_script_panel())


@app.patch("/api/script")
def api_script_patch() -> Any:
    bootstrap_once()
    payload = request.get_json(silent=True) or {}
    text = payload.get("script_text")
    if not isinstance(text, str):
        return jsonify({"error": "script_text must be string"}), 400
    return jsonify(write_script_text(text))


@app.get("/api/runs")
def api_runs() -> Any:
    bootstrap_once()
    reconcile_trigger_jobs()
    return jsonify({"runs": list_runs()})


@app.get("/api/runs/<run_id>")
def api_run_detail(run_id: str) -> Any:
    bootstrap_once()
    payload = read_payload_by_run_id(run_id)
    if payload is None:
        return jsonify({"error": "run not found"}), 404
    return jsonify(payload)


@app.get("/api/runs/<run_id>/download")
def api_run_download(run_id: str) -> Any:
    bootstrap_once()
    payload = read_payload_by_run_id(run_id)
    if payload is None:
        return jsonify({"error": "run not found"}), 404
    filename = f"{run_id}.json"
    raw = json.dumps(payload, indent=2, ensure_ascii=True).encode("utf-8")
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(raw, headers=headers, mimetype="application/json")


@app.get("/api/runs/latest/download")
def api_latest_run_download() -> Any:
    bootstrap_once()
    path = latest_payload_path()
    if path is None or not path.exists():
        return jsonify({"error": "No payload file available"}), 404
    return stream_file_response(path, download_name=path.name, mimetype="application/json")


@app.get("/api/scenes/<scene_id>/download/<asset_kind>")
def api_scene_asset_download(scene_id: str, asset_kind: str) -> Any:
    bootstrap_once()
    scene = get_scene(scene_id)
    if scene is None:
        return jsonify({"error": "scene not found"}), 404

    normalized = asset_kind.strip().lower()
    if normalized not in {"image", "video"}:
        return jsonify({"error": "asset_kind must be image or video"}), 400

    url = scene.get("image_url") if normalized == "image" else scene.get("video_url")
    if not isinstance(url, str) or not url.strip():
        return jsonify({"error": f"No {normalized} URL for scene {scene_id}"}), 404
    if is_simulated_url(url):
        return jsonify({"error": "Dry-run assets are simulated and cannot be downloaded"}), 400

    ext_guess = mimetypes.guess_extension(mimetypes.guess_type(url)[0] or "") or (".png" if normalized == "image" else ".mp4")
    safe_ext = ".bin" if ext_guess == ".jpe" else ext_guess
    download_name = f"{scene_id}_{normalized}{safe_ext}"
    try:
        return stream_remote_url_response(url, download_name=download_name)
    except requests.HTTPError as exc:
        return jsonify({"error": f"Download failed: {exc}"}), 502
    except requests.RequestException as exc:
        return jsonify({"error": f"Network error while downloading asset: {exc}"}), 502


@app.get("/api/jobs")
def api_trigger_jobs() -> Any:
    bootstrap_once()
    reconcile_trigger_jobs()
    return jsonify({"jobs": list_trigger_jobs()})


@app.get("/api/jobs/<job_id>/log")
def api_trigger_job_log(job_id: str) -> Any:
    bootstrap_once()
    reconcile_trigger_jobs()
    jobs = {job["id"]: job for job in list_trigger_jobs(limit=200)}
    job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    log_path = pathlib.Path(job["log_path"])
    return jsonify({"job_id": job_id, "log": tail_log(log_path)})


@app.post("/api/trigger")
def api_trigger() -> Any:
    bootstrap_once()
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
    bootstrap_once()
    app.run(host="127.0.0.1", port=5055, debug=False)


if __name__ == "__main__":
    main()
