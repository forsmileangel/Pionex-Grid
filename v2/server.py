"""Localhost-only dashboard for the SQLite ledger."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

from .capture import V1_CREDENTIALS, capture_live, collect_snapshot
from .settlements import get_settlements, refresh as refresh_settlements, save_manual
from .live import REFRESH_LOCK, commit_live_to_daily, live_payload
from .store import (
    CaptureError,
    DEFAULT_DB,
    apply_event_decision,
    board_payload,
    daily_date_payload,
    daily_payload,
    days_payload,
    export_sheets,
    grid_history_payload,
    grids_index,
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
SERVER_LOG = ROOT.parent / "v2-data" / "dashboard-server.log"


def local_post_allowed(origin: str, referer: str, port: int) -> bool:
    """Only the dashboard page itself may POST. Blocks other websites hitting 127.0.0.1."""
    allowed = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://[::1]:{port}",
    }
    origin = (origin or "").strip().rstrip("/")
    if origin in allowed:
        return True
    referer = (referer or "").strip()
    if referer:
        for base in allowed:
            if referer == base or referer.startswith(base + "/") or referer.startswith(base + "?"):
                return True
    return False


def _write_log(message: str) -> None:
    line = message if message.endswith("\n") else message + "\n"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"{stamp} {line}" if not line.startswith("20") else line
    try:
        SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SERVER_LOG.open("a", encoding="utf-8") as fh:
            fh.write(text)
    except Exception:
        pass
    try:
        import sys
        stream = sys.stderr or sys.stdout
        if stream is not None:
            stream.write(line)
            stream.flush()
    except Exception:
        return


class Handler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB

    def log_message(self, format, *args):
        _write_log("%s - %s\n" % (self.address_string(), format % args))

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
        try:
            self._handle_get()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, ConnectionError):
            return
        except Exception as exc:
            _write_log(f"GET {self.path} error: {exc}")
            try:
                self._json({"error": str(exc)}, 500)
            except Exception:
                return

    def _handle_get(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/":
            self._file(STATIC / "index.html", "text/html; charset=utf-8")
            return
        if path == "/architecture.html":
            self._file(STATIC / "architecture.html", "text/html; charset=utf-8")
            return
        if path == "/api/v1/status":
            self._json(status_payload(self.db_path))
            return
        if path == "/api/v1/board":
            self._json(board_payload(self.db_path))
            return
        if path == "/api/v1/live":
            self._json(live_payload())
            return
        if path == "/api/v1/settlements":
            self._json(get_settlements(self.db_path))
            return
        if path == "/api/v1/days":
            self._json(days_payload(self.db_path))
            return
        if path == "/api/v1/grids-index":
            self._json(grids_index(self.db_path))
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

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > 100_000:
            raise ValueError("body too large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _post_allowed(self) -> bool:
        port = int(self.server.server_address[1])
        return local_post_allowed(self.headers.get("Origin") or "", self.headers.get("Referer") or "", port)

    def do_POST(self):
        if not self._post_allowed():
            self._json({"error": "forbidden"}, 403)
            return
        try:
            self._handle_post()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, ConnectionError):
            return
        except Exception as exc:
            _write_log(f"POST {self.path} error: {exc}")
            try:
                self._json({"error": str(exc)}, 500)
            except Exception:
                return

    def _handle_post(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/api/v1/settlements/refresh" or path.startswith("/api/v1/settlements/"):
            if not REFRESH_LOCK.acquire(blocking=False):
                self._json({"error": "refresh already running"}, 409)
                return
            try:
                if path == "/api/v1/settlements/refresh":
                    result = refresh_settlements(collect_snapshot(V1_CREDENTIALS), self.db_path)
                else:
                    result = save_manual(unquote(path[len('/api/v1/settlements/'):]), self._read_json_body(), self.db_path)
                from .gist_publish import publish_ledger
                try:
                    result['publish'] = publish_ledger(self.db_path)
                except Exception as exc:
                    result['publish'] = {'ok': False, 'error': str(exc)}
                self._json(result)
            except (CaptureError, ValueError) as exc:
                self._json({'error': str(exc)}, 400)
            finally:
                REFRESH_LOCK.release()
            return
        if path == "/api/v1/live/refresh":
            if not REFRESH_LOCK.acquire(blocking=False):
                self._json({"error": "refresh already running"}, 409)
                return
            try:
                result = capture_live(V1_CREDENTIALS)
                self._json(result["board"])
            except CaptureError as exc:
                self._json({"error": str(exc)}, 500)
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
            finally:
                REFRESH_LOCK.release()
            return
        if path == "/api/v1/live/commit-daily":
            try:
                body = self._read_json_body()
            except Exception:
                self._json({"error": "invalid json"}, 400)
                return
            confirm = str((body or {}).get("confirm_date") or "")
            try:
                result = commit_live_to_daily(confirm, self.db_path)
                self._json({"ok": True, "capture_date": result.get("capture_date"), "publish": result.get("publish")})
            except CaptureError as exc:
                self._json({"error": str(exc)}, 400)
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
            return
        if path == "/api/v1/event-decision":
            try:
                body = self._read_json_body()
            except Exception:
                self._json({"error": "invalid json"}, 400)
                return
            try:
                result = apply_event_decision(
                    str((body or {}).get("capture_date") or ""),
                    str((body or {}).get("bu_order_id") or ""),
                    str((body or {}).get("decision") or ""),
                    self.db_path,
                )
                try:
                    from .gist_publish import publish_ledger
                    result["publish"] = publish_ledger(self.db_path)
                except Exception as exc:
                    result["publish"] = {"ok": False, "error": str(exc)}
                self._json(result)
            except CaptureError as exc:
                self._json({"error": str(exc)}, 400)
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
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
    try:
        httpd = ThreadingHTTPServer((HOST, args.port), Handler)
    except OSError as exc:
        text = str(exc).lower()
        if getattr(exc, "winerror", None) == 10048 or "already in use" in text or "address already in use" in text:
            _write_log(f"Pionex Grid SQLite dashboard already running at http://{HOST}:{args.port}")
            return
        raise
    _write_log(f"Pionex Grid SQLite dashboard http://{HOST}:{args.port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
