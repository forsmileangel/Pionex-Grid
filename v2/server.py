"""Localhost-only read dashboard for the v2 SQLite ledger."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .store import (
    DEFAULT_DB,
    daily_date_payload,
    daily_payload,
    export_sheets,
    grid_history_payload,
    grids_payload,
    status_payload,
    summary_latest,
)
from .xlsx import write_xlsx

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
EXPORTS = ROOT.parent / "exports"
HOST = "127.0.0.1"
PORT = 8787


class Handler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB

    def log_message(self, format, *args):
        sys_stderr = __import__("sys").stderr
        sys_stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def _json(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/":
            self._file(STATIC / "index.html", "text/html; charset=utf-8")
            return
        if path == "/api/v1/status":
            self._json(status_payload(self.db_path))
            return
        if path == "/api/v1/daily":
            self._json(daily_payload(self.db_path))
            return
        if path.startswith("/api/v1/daily/"):
            date = path.split("/api/v1/daily/", 1)[1]
            self._json(daily_date_payload(date, self.db_path))
            return
        if path == "/api/v1/grids":
            self._json(grids_payload(self.db_path))
            return
        if path.startswith("/api/v1/grids/") and path.endswith("/history"):
            grid_id = path[len("/api/v1/grids/") : -len("/history")]
            self._json(grid_history_payload(grid_id, self.db_path))
            return
        if path == "/api/v1/summary/latest":
            self._json(summary_latest(self.db_path))
            return
        if path == "/api/v1/export.xlsx":
            qs = parse_qs(parsed.query)
            name = (qs.get("name") or ["pionex-grid-export.xlsx"])[0]
            out = EXPORTS / Path(name).name
            write_xlsx(out, export_sheets(self.db_path))
            data = out.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="{out.name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(403)
        self.end_headers()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    Handler.db_path = Path(args.db)
    httpd = ThreadingHTTPServer((HOST, args.port), Handler)
    print(f"Pionex Grid v2 dashboard http://{HOST}:{args.port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
