#!/usr/bin/env python3
"""Vercel endpoint: fetch the latest public BTC trade ticks and paper-backtest them."""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.btc_tick_live import capture_live_ticks, write_outputs  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        try:
            # seconds=0 still retrieves the newest 1,000 public trades. Every row
            # remains a real exchange trade with its own timestamp and trade ID.
            ticks, metadata = capture_live_ticks(0)
            if len(ticks) < 2:
                raise RuntimeError(f"insufficient trades: {len(ticks)}")

            payload = write_outputs(ticks, metadata, Path("/tmp/btc-live-ticks"))
            payload["ticks"] = [asdict(tick) for tick in ticks]
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            body = json.dumps(
                {"error": type(exc).__name__, "message": str(exc)},
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
