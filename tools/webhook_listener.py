#!/usr/bin/env python3
"""Minimal webhook listener for triggering Phase 5 runs."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import pathlib
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional


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


def verify_signature(raw_body: bytes, webhook_id: str, ts: str, signature_header: str, secret: str) -> bool:
    clean_secret = secret.replace("whsec_", "", 1)
    signed_payload = f"{webhook_id}.{ts}.".encode("utf-8") + raw_body
    computed = hmac.new(clean_secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    # Accept either raw signature value or provider-formatted pairs.
    provided_candidates = [part.strip() for part in signature_header.split(",") if part.strip()]
    if signature_header.startswith("v1="):
        provided_candidates = [p.split("=", 1)[1] for p in provided_candidates if "=" in p]
    return any(hmac.compare_digest(computed, item) for item in provided_candidates)


class Handler(BaseHTTPRequestHandler):
    def _respond(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ("/webhook", "/wavespeed/webhook"):
            self._respond(HTTPStatus.NOT_FOUND, {"ok": False, "error": "unknown path"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        secret = os.getenv("WAVESPEED_WEBHOOK_SECRET", "")
        if secret:
            webhook_id = self.headers.get("webhook-id", "")
            webhook_ts = self.headers.get("webhook-timestamp", "")
            signature = self.headers.get("webhook-signature", "")
            if not webhook_id or not webhook_ts or not signature:
                self._respond(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "missing webhook signature headers"})
                return
            if not verify_signature(raw_body, webhook_id, webhook_ts, signature, secret):
                self._respond(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "invalid webhook signature"})
                return

        try:
            event = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except json.JSONDecodeError:
            self._respond(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid json"})
            return

        trigger_type = event.get("event") or event.get("type") or "manual_webhook"
        root = pathlib.Path(__file__).resolve().parents[1]
        cmd = [os.environ.get("PYTHON", "python3"), "tools/run_phase5_trigger.py"]
        if event.get("dry_run", False):
            cmd.append("--dry-run")
        # Fire-and-forget trigger. Status tracking goes to .tmp/logs by trigger script.
        subprocess.Popen(cmd, cwd=str(root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        self._respond(
            HTTPStatus.ACCEPTED,
            {
                "ok": True,
                "status": "accepted",
                "trigger": trigger_type,
                "message": "Phase 5 trigger started in background.",
            },
        )

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._respond(HTTPStatus.OK, {"ok": True, "service": "phase5-webhook-listener"})
            return
        self._respond(HTTPStatus.NOT_FOUND, {"ok": False, "error": "unknown path"})

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local webhook listener for Phase 5 triggers.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8787, type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env_file(pathlib.Path(".env").resolve())
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Webhook listener running on http://{args.host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
