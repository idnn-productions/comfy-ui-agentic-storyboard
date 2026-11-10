"""Background jobs started from the board: generate frames and render shots, one at a time.

  frames  `storyboard frames <board> --shots <ids> [--style sketch|photo] [--missing]` through ComfyUI (always available)
  render  the command in STORYBOARD_RENDER_CMD (or --render-cmd), only when it is configured; it is how the board
          hands a shot to your renderer. Placeholders: {board}, {shots} (comma separated), {seeds}. Example (the lab):
            "C:/Python313/python.exe" "C:/lab/run_board.py" render {board} --shots {shots} --seeds {seeds}

Jobs run in one worker thread, in order, so two GPU jobs never overlap. Each job keeps its last output lines for the
page. The command comes only from the server's configuration and validated names (board, shot ids, seeds, style),
never from free text sent by the page. A finished job changes the board on disk; the page reloads it by itself.
"""
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from collections import deque

from . import board as bd

SEEDS_RE = re.compile(r"^\d{1,9}(,\d{1,9}){0,7}$")
MAX_JOBS_KEPT = 50


class Jobs:
    def __init__(self, cfg, render_cmd=None):
        self.cfg = cfg
        self.render_cmd = render_cmd if render_cmd is not None else os.environ.get("STORYBOARD_RENDER_CMD", "")
        self.q = queue.Queue()
        self.jobs = {}
        self.order = deque()
        self.lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True).start()

    # ---- building commands (only validated parts go in)
    def _frames_cmd(self, board, shots, style, missing):
        cmd = [sys.executable, "-m", "storyboard", "--root", str(self.cfg.root), "--boards", str(self.cfg.boards),
               "--comfy", self.cfg.comfy_url, "frames", board]
        if shots:
            cmd += ["--shots", ",".join(shots)]
        if style:
            cmd += ["--style", style]
        if missing:
            cmd += ["--missing"]
        return cmd

    def _render_cmd(self, board, shots, seeds):
        parts = shlex.split(self.render_cmd, posix=os.name != "nt")
        parts = [p.strip('"') for p in parts]
        return [p.format(board=board, shots=",".join(shots), seeds=seeds) for p in parts]

    def submit(self, action, board, shots=(), style=None, missing=False, seeds="1"):
        if not bd.valid_name(board) or not self.cfg.board_file(board).exists():
            raise ValueError(f"no board {board!r}")
        shots = [s for s in shots]
        if any(not bd.SHOT_ID_RE.match(s) for s in shots):
            raise ValueError("bad shot id")
        if action == "frames":
            if style not in (None, "sketch", "photo"):
                raise ValueError("style must be sketch or photo")
            cmd = self._frames_cmd(board, shots, style, missing)
        elif action == "render":
            if not self.render_cmd:
                raise ValueError("rendering is not configured on this server (STORYBOARD_RENDER_CMD)")
            if not shots:
                raise ValueError("render needs shot ids")
            if not SEEDS_RE.match(seeds or ""):
                raise ValueError("seeds: up to 8 numbers, comma separated")
            cmd = self._render_cmd(board, shots, seeds)
        else:
            raise ValueError("action must be frames or render")
        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "action": action, "board": board, "shots": shots, "status": "queued",
               "created": time.strftime("%H:%M:%S"), "log": deque(maxlen=12), "returncode": None}
        with self.lock:
            self.jobs[jid] = job
            self.order.append(jid)
            while len(self.order) > MAX_JOBS_KEPT:
                self.jobs.pop(self.order.popleft(), None)
        self.q.put((jid, cmd))
        return self.view(job)

    def _worker(self):
        while True:
            jid, cmd = self.q.get()
            job = self.jobs.get(jid)
            if not job:
                continue
            job["status"] = "running"
            job["started"] = time.strftime("%H:%M:%S")
            env = {**os.environ, "PYTHONIOENCODING": "utf-8", "STORYBOARD_ROOT": str(self.cfg.root),
                   "STORYBOARD_BOARDS": str(self.cfg.boards)}
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                     errors="replace", env=env)
                for line in p.stdout:
                    line = line.rstrip()
                    if line:
                        job["log"].append(line[:300])
                job["returncode"] = p.wait()
                job["status"] = "done" if job["returncode"] == 0 else "failed"
            except OSError as e:
                job["log"].append(f"could not start: {e}")
                job["status"] = "failed"
            job["finished"] = time.strftime("%H:%M:%S")

    @staticmethod
    def view(job):
        return {k: (list(v) if isinstance(v, deque) else v) for k, v in job.items()}

    def list(self, board=None):
        with self.lock:
            return [self.view(self.jobs[j]) for j in self.order if board is None or self.jobs[j]["board"] == board]
