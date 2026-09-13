from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler

from promiseledger.demo_api import list_scenarios, run_scenario


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        self._json(200, {"scenarios": list_scenarios()})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode() or "{}")
            name = payload.get("scenario") or payload.get("id")
            if not name:
                raise ValueError("scenario is required")
            self._json(200, run_scenario(name))
        except ValueError as error:
            self._json(400, {"error": str(error)})
        except Exception as error:  # pragma: no cover
            self._json(500, {"error": str(error)})

    def log_message(self, format: str, *args: object) -> None:
        return

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)
