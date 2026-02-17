#!/usr/bin/env python3
"""APRT Phase 5 local dashboard (Airtable-style UI + trigger controls)."""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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
ACTIVE_JOBS: Dict[str, Dict[str, Any]] = {}
ACTIVE_LOCK = threading.Lock()


app = Flask(__name__, template_folder="templates", static_folder="static")


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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    ensure_dirs()
    with db_conn() as conn:
        conn.execute(
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
            )
            """
        )


def insert_job(job: Dict[str, Any]) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            insert into trigger_jobs (id, requested_at, mode, provider, status, pid, log_path)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job["id"],
                job["requested_at"],
                job["mode"],
                job["provider"],
                job["status"],
                job["pid"],
                job["log_path"],
            ),
        )


def update_job(job_id: str, *, status: str, exit_code: Optional[int]) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            update trigger_jobs
            set status = ?, exit_code = ?, finished_at = ?
            where id = ?
            """,
            (status, exit_code, utc_now(), job_id),
        )


def list_jobs(limit: int = 40) -> List[Dict[str, Any]]:
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
    schedules = []
    if isinstance(on_section, dict):
        schedule_items = on_section.get("schedule", [])
        if isinstance(schedule_items, list):
            for item in schedule_items:
                if isinstance(item, dict) and isinstance(item.get("cron"), str):
                    schedules.append(item["cron"])
    return schedules


def load_local_cron_entries() -> List[str]:
    try:
        proc = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    lines = []
    for line in proc.stdout.splitlines():
        normalized = line.strip()
        if not normalized or normalized.startswith("#"):
            continue
        if "run_phase5_trigger.py" in normalized:
            lines.append(normalized)
    return lines


def discover_script_path() -> pathlib.Path:
    if PAYLOAD_CONFIG_PATH.exists():
        try:
            payload = json.loads(PAYLOAD_CONFIG_PATH.read_text(encoding="utf-8"))
            raw = payload.get("voiceover_script_path")
            if isinstance(raw, str) and raw.strip():
                path = pathlib.Path(raw.strip()).expanduser()
                if not path.is_absolute():
                    path = ROOT / path
                return path.resolve()
        except Exception:
            pass
    return DEFAULT_SCRIPT_PATH.resolve()


def read_script() -> Dict[str, Any]:
    script_path = discover_script_path()
    if not script_path.exists():
        return {"script_path": str(script_path), "script_text": ""}
    return {
        "script_path": str(script_path),
        "script_text": script_path.read_text(encoding="utf-8"),
    }


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


def list_runs(limit: int = 150) -> List[Dict[str, Any]]:
    if not RUN_DIR.exists():
        return []
    payloads = sorted(
        RUN_DIR.glob("payload_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    rows: List[Dict[str, Any]] = []
    for payload_path in payloads[:limit]:
        summary = summarize_payload(payload_path)
        if summary:
            rows.append(summary)
    return rows


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


def tail_log(path: pathlib.Path, max_lines: int = 180) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])


def reconcile_jobs() -> None:
    with ACTIVE_LOCK:
        done_ids: List[str] = []
        for job_id, meta in ACTIVE_JOBS.items():
            proc: subprocess.Popen[Any] = meta["proc"]
            code = proc.poll()
            if code is None:
                continue
            handle = meta.get("log_handle")
            if handle:
                handle.close()
            status = "completed" if code == 0 else "failed"
            update_job(job_id, status=status, exit_code=code)
            done_ids.append(job_id)
        for job_id in done_ids:
            ACTIVE_JOBS.pop(job_id, None)


@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.get("/api/overview")
def api_overview() -> Any:
    reconcile_jobs()
    workflow_crons = load_workflow_crons()
    local_crons = load_local_cron_entries()
    return jsonify(
        {
            "triggers": {
                "github_workflow": {
                    "name": "phase5-story3-trigger",
                    "workflow_path": str(WORKFLOW_PATH.resolve()),
                    "schedule_cron_utc": workflow_crons,
                },
                "local_webhook": {
                    "listener_script": str((ROOT / "tools" / "webhook_listener.py").resolve()),
                    "path": "/webhook",
                    "health_path": "/health",
                },
                "local_cron": {
                    "installer_script": str((ROOT / "tools" / "install_local_cron.sh").resolve()),
                    "entries": local_crons,
                },
            },
            "script_panel": read_script(),
            "active_jobs": len(ACTIVE_JOBS),
        }
    )


@app.get("/api/runs")
def api_runs() -> Any:
    reconcile_jobs()
    return jsonify({"runs": list_runs()})


@app.get("/api/runs/<run_id>")
def api_run_detail(run_id: str) -> Any:
    data = read_payload_by_run_id(run_id)
    if data is None:
        return jsonify({"error": "run not found"}), 404
    return jsonify(data)


@app.get("/api/jobs")
def api_jobs() -> Any:
    reconcile_jobs()
    return jsonify({"jobs": list_jobs()})


@app.get("/api/jobs/<job_id>/log")
def api_job_log(job_id: str) -> Any:
    reconcile_jobs()
    jobs = {job["id"]: job for job in list_jobs(limit=200)}
    job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    log_path = pathlib.Path(job["log_path"])
    return jsonify({"job_id": job_id, "log": tail_log(log_path)})


@app.post("/api/trigger")
def api_trigger() -> Any:
    payload = request.get_json(silent=True) or {}
    dry_run = bool(payload.get("dry_run", True))
    provider = str(payload.get("provider", "auto")).strip().lower()
    if provider not in {"auto", "supabase", "cloudinary"}:
        return jsonify({"error": "provider must be auto, supabase, or cloudinary"}), 400

    reconcile_jobs()
    job_id = f"job-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
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

    env = os.environ.copy()
    log_handle = log_path.open("a", encoding="utf-8")
    proc = subprocess.Popen(  # noqa: S603
        cmd,
        cwd=str(ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        env=env,
    )

    with ACTIVE_LOCK:
        ACTIVE_JOBS[job_id] = {
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
    insert_job(job_record)
    return jsonify(job_record), 202


def main() -> None:
    load_env_file(ROOT / ".env")
    init_db()
    app.run(host="127.0.0.1", port=5055, debug=False)


if __name__ == "__main__":
    main()
