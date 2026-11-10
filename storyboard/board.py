"""Board storage: one folder per board under the boards directory, holding storyboard.json, frames/, audio/, history/.

Paths stored inside a board (frames, audio, takes) are relative to the configured root, so a board keeps working when
the root moves. Everything here is stdlib only.
"""
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
STATUSES = ["draft", "frames", "approved", "rendered", "picked"]
HISTORY_KEEP = 100
TRASH = "_trash"


@dataclass
class Config:
    root: Path         # base for the relative file paths stored in boards (often ComfyUI's input/output parent)
    boards: Path       # where board folders live; must be inside root
    comfy_url: str     # ComfyUI server, used by `frames`

    @classmethod
    def from_env(cls, root=None, boards=None, comfy_url=None):
        r = Path(root or os.environ.get("STORYBOARD_ROOT") or Path.cwd()).resolve()
        b = Path(boards or os.environ.get("STORYBOARD_BOARDS") or r / "storyboards").resolve()
        if not b.is_relative_to(r):
            raise SystemExit(f"boards directory {b} must be inside root {r}")
        return cls(r, b, (comfy_url or os.environ.get("COMFY_URL") or "http://127.0.0.1:8188").rstrip("/"))

    def board_dir(self, name):
        return self.boards / name

    def board_file(self, name):
        return self.boards / name / "storyboard.json"

    def rel(self, p):
        return Path(p).resolve().relative_to(self.root).as_posix()

    def resolve(self, rel_path):
        """Board-relative path -> absolute path, or None if it points outside the root."""
        p = (self.root / rel_path).resolve()
        return p if p.is_relative_to(self.root) else None


def valid_name(name):
    return bool(NAME_RE.match(name or ""))


def version_of(raw):
    return hashlib.sha1(raw).hexdigest()[:16]


def names(cfg):
    return sorted(p.parent.name for p in cfg.boards.glob("*/storyboard.json"))


def load(cfg, name):
    raw = cfg.board_file(name).read_bytes()
    return json.loads(raw.decode("utf-8")), version_of(raw)


def current_version(cfg, name):
    return version_of(cfg.board_file(name).read_bytes())


def save(cfg, name, board):
    """Write atomically; the previous version goes to history/ (last HISTORY_KEEP kept). Returns the new version."""
    p = cfg.board_file(name)
    raw = (json.dumps(board, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    if p.exists():
        hist = p.parent / "history"
        hist.mkdir(exist_ok=True)
        shutil.copy2(p, hist / time.strftime("storyboard_%Y%m%d_%H%M%S.json"))
        for f in sorted(hist.glob("storyboard_*.json"))[:-HISTORY_KEEP]:
            f.unlink()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(raw)
    tmp.replace(p)
    return version_of(raw)


def timings(board):
    """[(start, end)] per shot: shots play back to back from audio.offset, in array order."""
    t = float((board.get("audio") or {}).get("offset", 0.0))
    out = []
    for s in board["shots"]:
        d = float(s.get("duration", 0))
        out.append((t, t + d))
        t += d
    return out


def create(cfg, name, title=None, kind="story", width=1280, height=720, brief="", request="", audio=None, bpm=None,
           first_beat=0.0):
    if not valid_name(name):
        raise ValueError("name: lowercase letters, digits, - and _ only (max 64)")
    if cfg.board_file(name).exists():
        raise ValueError(f"board {name} exists")
    for sub in ("frames", "audio"):
        (cfg.board_dir(name) / sub).mkdir(parents=True, exist_ok=True)
    board = {"title": title or name, "kind": kind, "fps": 24, "width": int(width), "height": int(height),
             "frame_style": "sketch", "brief": brief or "", "look": "", "characters": [], "notes": "", "shots": []}
    if request:
        board["request"] = request
    if audio:
        board["audio"] = {"file": audio, "offset": 0.0}
        if bpm:
            board["audio"].update(bpm=float(bpm), first_beat=float(first_beat))
    save(cfg, name, board)
    return board


def rename(cfg, old, new, copy=False):
    """Rename (or duplicate) a board folder and rewrite the paths inside it that pointed into the old folder."""
    if not valid_name(new):
        raise ValueError("name: lowercase letters, digits, - and _ only (max 64)")
    if not cfg.board_file(old).exists():
        raise ValueError(f"no board {old}")
    if cfg.board_file(new).exists():
        raise ValueError(f"board {new} exists")
    src, dst = cfg.board_dir(old), cfg.board_dir(new)
    if copy:
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("history"))
    else:
        src.rename(dst)
    prefix = cfg.rel(cfg.boards)
    p = cfg.board_file(new)
    p.write_text(p.read_text(encoding="utf-8").replace(f"{prefix}/{old}/", f"{prefix}/{new}/"), encoding="utf-8")


def delete(cfg, name):
    """Move a board to <boards>/_trash/<name>_<time>/ (restore by moving it back). Returns the trash folder."""
    if not cfg.board_file(name).exists():
        raise ValueError(f"no board {name}")
    trash = cfg.boards / TRASH
    trash.mkdir(exist_ok=True)
    dst = trash / f"{name}_{time.strftime('%Y%m%d_%H%M%S')}"
    cfg.board_dir(name).rename(dst)
    return dst


def summary(cfg, name):
    try:
        b, _ = load(cfg, name)
    except (OSError, ValueError):
        return None
    thumb = next((s["start_frame"] for s in b["shots"] if s.get("start_frame")), None)
    reqs = (1 if b.get("request") else 0) + sum(1 for s in b["shots"] if s.get("request"))
    return {"name": name, "title": b.get("title", name), "kind": b.get("kind", "story"), "shots": len(b["shots"]),
            "seconds": round(sum(float(s.get("duration", 0)) for s in b["shots"]), 1), "thumb": thumb,
            "requests": reqs,
            "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(cfg.board_file(name).stat().st_mtime))}


def requests(cfg, name=None):
    """Open requests for the agent: [(board, shot_id or None, text)]."""
    out = []
    for n in [name] if name else names(cfg):
        b, _ = load(cfg, n)
        if b.get("request"):
            out.append((n, None, b["request"]))
        out += [(n, s["id"], s["request"]) for s in b["shots"] if s.get("request")]
    return out
