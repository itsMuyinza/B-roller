#!/usr/bin/env python3
"""Unified trigger entrypoint for cron, webhooks, and cloud workflows."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone
from typing import Dict


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_cmd(cmd: list[str], cwd: pathlib.Path) -> Dict[str, str]:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "Command failed:\n"
            f"cmd={' '.join(cmd)}\n"
            f"code={proc.returncode}\n"
            f"stdout={proc.stdout}\n"
            f"stderr={proc.stderr}"
        )
    stdout = proc.stdout.strip()
    return {"stdout": stdout, "stderr": proc.stderr.strip()}


def parse_json_stdout(stdout: str) -> Dict[str, str]:
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Expected JSON stdout, got: {stdout}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Expected JSON object stdout, got: {parsed}")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run full Phase 5 trigger pipeline.")
    parser.add_argument("--input", default="tools/config/script_3_hoodrat_payload.json")
    parser.add_argument("--out-dir", default=".tmp/phase5_story3")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-cloud-transfer", action="store_true")
    parser.add_argument(
        "--provider",
        choices=["auto", "supabase", "cloudinary"],
        default="auto",
        help="Cloud transfer provider selection.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = pathlib.Path.cwd().resolve()
    out_dir = pathlib.Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = (root / ".tmp" / "logs").resolve()
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"phase5_trigger_{utc_stamp()}.log"

    pipeline_cmd = [
        sys.executable,
        "tools/wavespeed_story_pipeline.py",
        "--input",
        args.input,
        "--out-dir",
        str(out_dir),
    ]
    if args.dry_run:
        pipeline_cmd.append("--dry-run")
    pipeline_result = run_cmd(pipeline_cmd, cwd=root)
    pipeline_json = parse_json_stdout(pipeline_result["stdout"])

    payload_path = pipeline_json.get("output_path")
    if not payload_path:
        raise RuntimeError(f"Pipeline did not return output_path. Payload: {pipeline_json}")

    transfer_json: Dict[str, str] = {
        "status": "skipped",
        "reason": "skip-cloud-transfer flag enabled",
    }
    if not args.skip_cloud_transfer:
        transfer_cmd = [
            sys.executable,
            "tools/cloud_transfer.py",
            "--payload",
            payload_path,
            "--provider",
            args.provider,
        ]
        if args.dry_run:
            transfer_cmd.append("--dry-run")
        transfer_result = run_cmd(transfer_cmd, cwd=root)
        transfer_json = parse_json_stdout(transfer_result["stdout"])

    summary = {
        "pipeline": pipeline_json,
        "cloud_transfer": transfer_json,
    }
    log_path.write_text(json.dumps(summary, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps({"status": "ok", "log": str(log_path), **summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
