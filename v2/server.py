"""Localhost-only dashboard for the SQLite ledger."""

from __future__ import annotations

import argparse
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

from .capture import V1_CREDENTIALS, capture_live, collect_snapshot, finished_cache_status
from .intraday import (
    EfficiencyError,
    default_intraday_db,
    efficiency_meta,
    efficiency_series,
    intraday_status,
    load_views,
    save_views,
    views_path,
)
from .settlements import audit as settlement_audit, get_settlements, refresh as refresh_settlements, save_manual
from .live import REFRESH_LOCK, commit_live_to_daily, live_payload, load_live_snapshot, save_live_snapshot
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
    sparkline_payload,
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


LIVE_PUBLISH_LOCK = threading.Lock()
_live_published = {"as_of": ""}


def publish_live_later(board: dict) -> threading.Thread:
    """Send the live board to the Gist after the page already has it. An older board that
    finishes after a newer one is skipped; failures land in publish-state.json as before."""
    def run():
        with LIVE_PUBLISH_LOCK:
            as_of = str(board.get("as_of") or "")
            if as_of and as_of < _live_published["as_of"]:
                return
            try:
                from .gist_publish import publish_live
                publish_live(board)
            except Exception:
                return
            _live_published["as_of"] = max(as_of, _live_published["as_of"])
    thread = threading.Thread(target=run, name="live-publish", daemon=True)
    thread.start()
    return thread


def clock_status(path: Path | None = None) -> dict:
    """How far this PC's clock was from Pionex's on the latest live capture (the collector corrects for it)."""
    try:
        snap = load_live_snapshot(path) or {}
    except Exception:
        snap = {}
    return {"offset_ms": snap.get("ClockOffsetMs"), "at": snap.get("CapturedAt")}


def status_with_publish(db_path: Path, intraday_db: Path | None = None) -> dict:
    """Status plus whether Portfolio Tracker has the current ledger."""
    from .gist_publish import LEDGER_FILENAME, LIVE_FILENAME, ledger_dirty, read_publish_credentials, read_publish_state
    payload = status_payload(db_path)
    try:
        payload["intraday"] = intraday_status(intraday_db or default_intraday_db(db_path))
    except Exception as exc:
        payload["intraday"] = {"available": False, "error": str(exc)}
    try:
        payload["finished_cache"] = finished_cache_status()
    except Exception as exc:
        payload["finished_cache"] = {"available": False, "error": str(exc)}
    payload["clock"] = clock_status()
    state = read_publish_state()
    publish = {
        "configured": bool(read_publish_credentials()),
        "ledger": state.get(LEDGER_FILENAME),
        "live": state.get(LIVE_FILENAME),
        "ledger_in_sync": None,
    }
    try:
        publish["ledger_in_sync"] = not ledger_dirty(db_path)
    except Exception as exc:
        publish["error"] = str(exc)
    payload["publish"] = publish
    return payload


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
    intraday_db = None

    def _intraday_path(self) -> Path:
        return Path(self.intraday_db) if self.intraday_db else default_intraday_db(self.db_path)

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
        if path == "/chart.js":
            self._file(STATIC / "chart.js", "text/javascript; charset=utf-8")
            return
        if path == "/api/v1/status":
            self._json(status_with_publish(self.db_path, self._intraday_path()))
            return
        if path == "/api/v1/efficiency":
            qs = parse_qs(parsed.query)
            arg = lambda key: (qs.get(key) or [""])[0]
            ids = [i for i in arg("ids").split(",") if i]
            try:
                self._json(efficiency_series(
                    arg("from") or None, arg("to") or None, arg("g") or "1h", ids or None,
                    extras=arg("extras") != "0", ledger_db=self.db_path, intraday_db=self._intraday_path(),
                ))
            except EfficiencyError as exc:
                self._json({"error": str(exc), "suggest": exc.suggest}, 400)
            return
        if path == "/api/v1/efficiency/meta":
            self._json(efficiency_meta(self.db_path, self._intraday_path()))
            return
        if path == "/api/v1/efficiency/views":
            self._json(load_views(views_path(self._intraday_path())))
            return
        if path == "/api/v1/efficiency/sparklines":
            qs = parse_qs(parsed.query)
            try:
                days = max(2, min(60, int((qs.get("days") or ["14"])[0])))
            except ValueError:
                days = 14
            self._json(sparkline_payload(days, self.db_path))
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
        if path.startswith("/api/v1/settlements/") and path.endswith("/audit"):
            oid = unquote(path[len("/api/v1/settlements/"):-len("/audit")])
            self._json({"rows": settlement_audit(oid, self.db_path)})
            return
        if path == "/api/v1/days":
            self._json(days_payload(self.db_path))
            return
        if path == "/api/v1/funding":
            from .funding import funding_payload
            self._json(funding_payload(self.db_path))
            return
        if path == "/api/v1/leverage-compare":
            from .levercompare import compare_payload
            self._json(compare_payload(self.db_path))
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
        if path == "/api/v1/efficiency/views":
            try:
                self._json(save_views(self._read_json_body(), views_path(self._intraday_path())))
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            return
        if path == "/api/v1/publish/ledger":
            from .gist_publish import publish_ledger
            try:
                # Drain the request body: answering with unread bytes makes Windows reset the socket.
                self._read_json_body()
            except Exception:
                pass
            try:
                result = publish_ledger(self.db_path)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            self._json(result, 200 if result.get("ok") or result.get("skipped") else 502)
            return
        if path == "/api/v1/live/refresh":
            if not REFRESH_LOCK.acquire(blocking=False):
                self._json({"error": "refresh already running"}, 409)
                return
            try:
                result = capture_live(V1_CREDENTIALS, source="manual", db_path=self.db_path,
                                      intraday_db=self._intraday_path(), skip_publish=True)
                self._json(result["board"])
                publish_live_later(result["board"])
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
            if not REFRESH_LOCK.acquire(blocking=False):
                self._json({"error": "refresh already running"}, 409)
                return
            try:
                # Same as the 08:05 capture: every bot read in full, no local closed-bot cache.
                save_live_snapshot(collect_snapshot(V1_CREDENTIALS))
                result = commit_live_to_daily(confirm, self.db_path)
                self._json({"ok": True, "capture_date": result.get("capture_date"), "publish": result.get("publish")})
            except CaptureError as exc:
                self._json({"error": str(exc)}, 400)
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
            finally:
                REFRESH_LOCK.release()
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
    parser.add_argument("--intraday-db", default="", help="Intraday points file (default: beside --db)")
    args = parser.parse_args()
    Handler.db_path = Path(args.db)
    Handler.intraday_db = Path(args.intraday_db) if args.intraday_db else None
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
