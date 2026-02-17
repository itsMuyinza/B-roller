"""Vercel entrypoint for APRT dashboard."""

import os

# Use writable ephemeral storage in serverless runtime.
os.environ.setdefault("DASHBOARD_DATA_ROOT", "/tmp")

from dashboard.app import app as app  # noqa: E402,F401

