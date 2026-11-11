"""The board web server: 127.0.0.1 only, stdlib http.server.

Routes
  GET  /                          the page (home without ?b=, a board with ?b=<name>)
  GET  /api/boards                board summaries
  GET  /api/board?b=<name>        {name, version, board, statuses}
  GET  /api/version?b=<name>      {version}  (the page polls this to pick up edits made on disk, e.g. by an agent)
  GET  /api/server                {stale, render}  (stale: package code changed after start; render: a render command is set)
  GET  /api/models               frame models: [{id, label, kind, note, usd_per_image, available, reason}]
  GET  /api/jobs?b=<name>         jobs for the board (frames / render), newest last, with their last log lines
  POST /api/jobs                  {action: frames | revise | render, b, shots: [ids], style?, missing?, count?,
                                   seeds?, revision?} -> job (revision: see revise.check_request)
  GET  /file?p=<root-relative>    any file under the root, with HTTP Range support (audio/video seeking)
  PUT  /api/board?b=<name>        {version, board} -> 409 if the file changed since that version
  POST /api/boards                {action: create | rename | duplicate | delete, ...}
  POST /api/upload?b=&shot=&slot=start|end|audio|take|reference|annotated|mask&name=   raw body -> {path}

Safety: GETs answer only Host 127.0.0.1/localhost (DNS rebinding); PUT/POST also need the header "X-Storyboard: 1",
which a cross-site page cannot send without a CORS preflight this server never approves. File reads are confined
to the root, uploads to the board's frames/ or audio/ folder.
"""
import http.server
import json
import mimetypes
import os
import secrets
import time
import urllib.parse
import webbrowser
from pathlib import Path

from . import board as bd
from .jobs import Jobs

PKG = Path(__file__).resolve().parent
PAGE = PKG / "web" / "board.html"
UPLOAD_EXT = {"start": (".png", ".jpg", ".jpeg", ".webp"), "end": (".png", ".jpg", ".jpeg", ".webp"),
              "audio": (".wav", ".mp3", ".flac", ".m4a", ".ogg"), "take": (".mp4", ".webm", ".mov"),
              "reference": (".mp4", ".webm", ".mov"), "annotated": (".png",), "mask": (".png",)}
UPLOAD_DIR = {"audio": "audio", "reference": "refs", "take": "takes", "annotated": "revisions", "mask": "revisions"}


def code_stamp():
    return max(p.stat().st_mtime for p in PKG.glob("*.py"))


def local_host(headers):
    return (headers.get("Host") or "").rsplit(":", 1)[0] in ("127.0.0.1", "localhost")


def make_handler(cfg, jobs=None):
    started = code_stamp()
    jobs = jobs or Jobs(cfg)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False)
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _board(self, q):
            name = q.get("b", [""])[0]
            if not bd.valid_name(name) or not cfg.board_file(name).exists():
                self._send(404, {"error": f"no board {name!r}"})
                return None
            return name

        def do_GET(self):
            if not local_host(self.headers):
                return self._send(403, {"error": "forbidden"})
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path == "/":
                # Only the page's own script runs (per-response nonce): a value from a board that slipped past
                # escaping still cannot execute script. Inline style attributes stay allowed (layout uses them).
                nonce = secrets.token_urlsafe(16)
                page = PAGE.read_text(encoding="utf-8").replace("<script>", f'<script nonce="{nonce}">', 1)
                csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; img-src 'self'; "
                       "media-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
                return self._send(200, page, "text/html; charset=utf-8", {"Content-Security-Policy": csp})
            if u.path == "/api/server":
                return self._send(200, {"stale": code_stamp() != started, "render": bool(jobs.render_cmd)})
            if u.path == "/api/jobs":
                return self._send(200, {"jobs": jobs.list(q.get("b", [None])[0])})
            if u.path == "/api/models":
                return self._send(200, {"models": jobs.models.all()})
            if u.path == "/api/boards":
                return self._send(200, {"boards": [s for s in (bd.summary(cfg, n) for n in bd.names(cfg)) if s]})
            if u.path == "/api/board":
                name = self._board(q)
                if name:
                    b, ver = bd.load(cfg, name)
                    self._send(200, {"name": name, "version": ver, "board": b, "statuses": bd.STATUSES})
                return None
            if u.path == "/api/version":
                name = self._board(q)
                if name:
                    self._send(200, {"version": bd.current_version(cfg, name)})
                return None
            if u.path == "/file":
                p = cfg.resolve(q.get("p", [""])[0])
                if not p or not p.is_file():
                    return self._send(404, "not found", "text/plain")
                return self._file(p)
            return self._send(404, "not found", "text/plain")

        def _file(self, p):
            size = p.stat().st_size
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            if rng and rng.startswith("bytes="):
                a0, _, a1 = rng[6:].partition("-")
                try:
                    start = int(a0) if a0 else max(0, size - int(a1))
                    end = min(int(a1) if a0 and a1 else size - 1, size - 1)
                except ValueError:
                    rng = None
                    start, end = 0, size - 1
            length = max(0, end - start + 1)
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with p.open("rb") as f:
                f.seek(start)
                left = length
                try:
                    while left > 0:
                        chunk = f.read(min(1 << 20, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                    pass

        def _same_origin(self):
            if self.headers.get("X-Storyboard") != "1" or not local_host(self.headers):
                self._send(403, {"error": "forbidden"})
                return False
            return True

        def _json_body(self):
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))

        def do_PUT(self):
            if not self._same_origin():
                return None
            u = urllib.parse.urlparse(self.path)
            if u.path != "/api/board":
                return self._send(404, "not found", "text/plain")
            name = self._board(urllib.parse.parse_qs(u.query))
            if not name:
                return None
            body = self._json_body()
            if body.get("version") != bd.current_version(cfg, name):
                return self._send(409, {"error": "changed on disk", "version": bd.current_version(cfg, name)})
            try:
                return self._send(200, {"version": bd.save(cfg, name, body.get("board"))})
            except ValueError as e:
                return self._send(400, {"error": str(e)})

        def do_POST(self):
            if not self._same_origin():
                return None
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path == "/api/boards":
                return self._boards_action(self._json_body())
            if u.path == "/api/jobs":
                body = self._json_body()
                try:
                    job = jobs.submit(body.get("action"), body.get("b", ""), body.get("shots") or [],
                                      body.get("style"), bool(body.get("missing")), str(body.get("seeds", "1")),
                                      body.get("count", 1), body.get("revision"))
                except (ValueError, TypeError) as e:
                    return self._send(400, {"error": str(e)})
                return self._send(200, {"job": job})
            if u.path == "/api/upload":
                return self._upload(q)
            return self._send(404, "not found", "text/plain")

        def _boards_action(self, body):
            act, name = body.get("action"), body.get("name", "")
            try:
                if act == "create":
                    bd.create(cfg, name, body.get("title"), body.get("kind", "story"), body.get("width", 1280),
                              body.get("height", 720), body.get("brief", ""), body.get("request", ""))
                    return self._send(200, {"name": name})
                if not bd.valid_name(name) or not cfg.board_file(name).exists():
                    return self._send(404, {"error": f"no board {name!r}"})
                if act in ("rename", "duplicate"):
                    bd.rename(cfg, name, body.get("new_name", ""), copy=act == "duplicate")
                    return self._send(200, {"name": body["new_name"]})
                if act == "delete":
                    return self._send(200, {"trash": cfg.rel(bd.delete(cfg, name))})
            except (ValueError, KeyError, OSError) as e:
                return self._send(400, {"error": str(e)})
            return self._send(400, {"error": "unknown action"})

        def _upload(self, q):
            name = self._board(q)
            if not name:
                return None
            shot = "".join(c for c in q.get("shot", ["x"])[0] if c.isalnum() or c in "-_")[:40] or "x"
            slot = q.get("slot", ["start"])[0]
            if slot not in UPLOAD_EXT:
                return self._send(400, {"error": "bad slot"})
            ext = Path(q.get("name", ["x.png"])[0]).suffix.lower()
            if ext not in UPLOAD_EXT[slot]:
                return self._send(400, {"error": f"{ext} not allowed for {slot}"})
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            d = (cfg.board_dir(name) / UPLOAD_DIR.get(slot, "frames")).resolve()
            d.mkdir(parents=True, exist_ok=True)
            f = (d / f"{shot}_{slot}_{time.strftime('%Y%m%d_%H%M%S')}{ext}").resolve()
            if not f.is_relative_to(d):
                return self._send(400, {"error": "bad path"})
            f.write_bytes(data)
            return self._send(200, {"path": cfg.rel(f)})

    return H


def serve(cfg, port=8770, name=None, open_browser=True, render_cmd=None):
    cfg.boards.mkdir(parents=True, exist_ok=True)
    class Server(http.server.ThreadingHTTPServer):
        # Windows' SO_REUSEADDR lets a second server bind a port that is in use and steal half the requests;
        # fail loudly instead.
        allow_reuse_address = os.name != "nt"

    try:
        srv = Server(("127.0.0.1", port), make_handler(cfg, Jobs(cfg, render_cmd)))
    except OSError as e:
        raise SystemExit(f"port {port} is busy ({e}); is a storyboard server already running? Try --port {port + 1}")
    url = f"http://127.0.0.1:{port}/" + (f"?b={urllib.parse.quote(name)}" if name else "")
    print(f"storyboards in {cfg.boards}\nopen {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
