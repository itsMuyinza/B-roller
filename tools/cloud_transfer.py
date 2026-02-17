#!/usr/bin/env python3
"""Transfer final payload artifacts to cloud storage/database."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests


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


def transfer_supabase(payload: Dict[str, Any], table: str, dry_run: bool) -> Dict[str, Any]:
    project_id = os.getenv("SUPABASE_PROJECT_ID", "")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not project_id or not service_key:
        return {
            "provider": "supabase",
            "status": "failed",
            "destination": table,
            "message": "Missing SUPABASE_PROJECT_ID or SUPABASE_SERVICE_ROLE_KEY.",
        }

    endpoint = f"https://{project_id}.supabase.co/rest/v1/{table}"
    row = {
        "run_id": payload.get("run_id"),
        "story_id": payload.get("story_id"),
        "status": payload.get("status"),
        "generated_at": payload.get("run", {}).get("ended_at") or utc_now_iso(),
        "payload": payload,
    }
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation,resolution=merge-duplicates",
    }

    if dry_run:
        return {
            "provider": "supabase",
            "status": "success",
            "destination": endpoint,
            "message": "Dry run only; no network write performed.",
        }

    response = requests.post(endpoint, headers=headers, json=[row], timeout=90)
    if not response.ok:
        message = response.text.strip()[:1000]
        return {
            "provider": "supabase",
            "status": "failed",
            "destination": endpoint,
            "message": f"Supabase write failed ({response.status_code}): {message}",
        }
    inserted = response.json()
    return {
        "provider": "supabase",
        "status": "success",
        "destination": endpoint,
        "message": "Payload inserted/upserted to Supabase.",
        "record": inserted[0] if isinstance(inserted, list) and inserted else inserted,
    }


def cloudinary_signature(params: Dict[str, Any], api_secret: str) -> str:
    flattened = "&".join(f"{key}={params[key]}" for key in sorted(params.keys()))
    return hashlib.sha1(f"{flattened}{api_secret}".encode("utf-8")).hexdigest()


def transfer_cloudinary(payload: Dict[str, Any], folder: str, dry_run: bool) -> Dict[str, Any]:
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "")
    api_key = os.getenv("CLOUDINARY_API_KEY", "")
    api_secret = os.getenv("CLOUDINARY_API_SECRET", "")
    if not cloud_name or not api_key or not api_secret:
        return {
            "provider": "cloudinary",
            "status": "failed",
            "destination": folder,
            "message": "Missing Cloudinary credentials.",
        }

    run_id = payload.get("run_id", "unknown")
    public_id = f"{folder.strip('/').replace('/', '_')}_{run_id}"
    endpoint = f"https://api.cloudinary.com/v1_1/{cloud_name}/raw/upload"

    if dry_run:
        return {
            "provider": "cloudinary",
            "status": "success",
            "destination": f"{endpoint}#{public_id}",
            "message": "Dry run only; no network write performed.",
        }

    timestamp = int(time.time())
    sign_params = {"folder": folder, "public_id": public_id, "timestamp": timestamp}
    signature = cloudinary_signature(sign_params, api_secret)
    data = {
        "api_key": api_key,
        "timestamp": timestamp,
        "folder": folder,
        "public_id": public_id,
        "signature": signature,
    }
    payload_bytes = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    files = {"file": (f"{run_id}.json", payload_bytes, "application/json")}

    response = requests.post(endpoint, data=data, files=files, timeout=90)
    if not response.ok:
        message = response.text.strip()[:1000]
        return {
            "provider": "cloudinary",
            "status": "failed",
            "destination": endpoint,
            "message": f"Cloudinary upload failed ({response.status_code}): {message}",
        }
    resp_json = response.json()
    return {
        "provider": "cloudinary",
        "status": "success",
        "destination": resp_json.get("secure_url") or endpoint,
        "message": "Payload uploaded as Cloudinary raw asset.",
        "record": resp_json,
    }


def write_json(path: pathlib.Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cloud transfer for APRT payload artifacts.")
    parser.add_argument("--payload", required=True, help="Path to payload JSON file.")
    parser.add_argument(
        "--provider",
        choices=["auto", "supabase", "cloudinary"],
        default="auto",
        help="Cloud provider target.",
    )
    parser.add_argument(
        "--supabase-table",
        default="aprt_story_payloads",
        help="Target Supabase table.",
    )
    parser.add_argument(
        "--cloudinary-folder",
        default="aprt/payloads",
        help="Cloudinary folder for raw payload uploads.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Skip network writes.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env_file(pathlib.Path(".env").resolve())

    payload_path = pathlib.Path(args.payload).expanduser().resolve()
    if not payload_path.exists():
        raise SystemExit(f"Payload file does not exist: {payload_path}")

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    provider = args.provider

    result: Optional[Dict[str, Any]] = None
    primary_result: Optional[Dict[str, Any]] = None
    if provider in {"auto", "supabase"}:
        table = args.supabase_table
        if isinstance(payload.get("output"), dict) and payload["output"].get("supabase_table"):
            table = payload["output"]["supabase_table"]
        primary_result = transfer_supabase(payload, table=table, dry_run=bool(args.dry_run))
        result = primary_result
        if provider == "supabase" or (result and result.get("status") == "success"):
            pass
        else:
            fallback = transfer_cloudinary(payload, folder=args.cloudinary_folder, dry_run=bool(args.dry_run))
            if fallback.get("status") == "success" and primary_result:
                fallback["message"] = (
                    f"{fallback.get('message', 'Cloudinary fallback success')} "
                    f"(Supabase fallback reason: {primary_result.get('message', 'unknown')})"
                )
                fallback["fallback_from"] = primary_result
            result = fallback
    elif provider == "cloudinary":
        result = transfer_cloudinary(payload, folder=args.cloudinary_folder, dry_run=bool(args.dry_run))

    if not result:
        result = {
            "provider": "unknown",
            "status": "failed",
            "destination": None,
            "message": "No provider result produced.",
        }

    cloud_transfer = {
        "provider": result.get("provider"),
        "status": result.get("status"),
        "destination": result.get("destination"),
        "message": result.get("message"),
        "transferred_at": utc_now_iso(),
    }
    payload["cloud_transfer"] = cloud_transfer
    if result.get("status") != "success":
        payload.setdefault("errors", []).append(
            {"stage": "cloud_transfer", "message": result.get("message", "Cloud transfer failed")}
        )

    write_json(payload_path, payload)
    print(json.dumps({"payload_path": str(payload_path), "cloud_transfer": cloud_transfer}, indent=2))
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
