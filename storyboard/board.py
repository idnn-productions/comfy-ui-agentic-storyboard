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
SHOT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")  # shot ids become file and folder names (frames, export)
KINDS = ("story", "music")
LANES = ("A", "B", "C", "D", "E")   # A dialogue, B character in a moving world, C cinematic, D photo, E music video
MOTIONS = ("dialogue", "ambient", "action", "music")
WORKFLOW_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
VOICE_ID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")
FRAME_STYLES = ("sketch", "photo")
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


def _num(v, lo=None, hi=None):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return (lo is None or v >= lo) and (hi is None or v <= hi)


def validate(board):
    """Raise ValueError unless the board is well formed. Every save goes through this (page and agent alike):
    shot ids become file and folder names, and board values end up in the page."""
    if not isinstance(board, dict) or not isinstance(board.get("shots"), list):
        raise ValueError("board needs a shots list")
    for k in ("width", "height"):
        if k in board and not (_num(board[k], 16, 8192) and float(board[k]).is_integer()):
            raise ValueError(f"{k} must be a whole number of pixels (16-8192)")
    if "fps" in board and not _num(board["fps"], 1, 240):
        raise ValueError("fps must be a number")
    if board.get("kind", "story") not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if board.get("frame_style", "sketch") not in FRAME_STYLES:
        raise ValueError(f"frame_style must be one of {FRAME_STYLES}")
    audio = board.get("audio")
    if audio is not None:
        if not isinstance(audio, dict) or not isinstance(audio.get("file", ""), str):
            raise ValueError("audio must be {file, offset, bpm, first_beat}")
        for k in ("offset", "bpm", "first_beat"):
            if k in audio and not _num(audio[k], 0):
                raise ValueError(f"audio.{k} must be a number >= 0")
    for c in board.get("characters") or []:
        if not isinstance(c, dict) or not isinstance(c.get("id"), str):
            raise ValueError("characters need string ids")
        for k in ("desc", "block"):
            if k in c and not isinstance(c[k], str):
                raise ValueError(f"character {c['id']}: {k} must be text")
        if c.get("voice_id") and not (isinstance(c["voice_id"], str) and VOICE_ID_RE.match(c["voice_id"])):
            raise ValueError(f"character {c['id']}: voice_id must be an ElevenLabs voice id (letters and digits)")
        if c.get("pack") and not (isinstance(c["pack"], str) and NAME_RE.match(c["pack"])):
            raise ValueError(f"character {c['id']}: pack must be a pack folder name (lowercase, digits, - _)")
    seen = set()
    for sh in board["shots"]:
        sid = sh.get("id") if isinstance(sh, dict) else None
        if not isinstance(sid, str) or not SHOT_ID_RE.match(sid):
            raise ValueError(f"bad shot id {sid!r}: 1-40 letters, digits, - and _")
        if sid in seen:
            raise ValueError(f"duplicate shot id {sid}")
        seen.add(sid)
        if not _num(sh.get("duration", 0), 0, 3600):
            raise ValueError(f"shot {sid}: duration must be a number of seconds")
        if sh.get("status", "draft") not in STATUSES:
            raise ValueError(f"shot {sid}: status must be one of {STATUSES}")
        for k in ("start_frame", "end_frame", "audio", "pick"):
            if k in sh and not isinstance(sh[k], str):
                raise ValueError(f"shot {sid}: {k} must be a path string")
        _check_production_fields(sh, sid)
        takes = sh.get("takes", [])
        if not (isinstance(takes, list) and all(isinstance(t, dict) and isinstance(t.get("path"), str) for t in takes)):
            raise ValueError(f"shot {sid}: takes must be a list of {{path, label}}")
    return board


def shot_motion(shot):
    """The shot's motion profile: its `motion` field, else `dialogue` for lane A and `ambient` otherwise."""
    return shot.get("motion") or ("dialogue" if shot.get("lane", "A") == "A" else "ambient")


def _check_production_fields(sh, sid):
    if "lane" in sh and sh["lane"] not in LANES:
        raise ValueError(f"shot {sid}: lane must be one of {LANES}")
    if "motion" in sh and sh["motion"] not in MOTIONS:
        raise ValueError(f"shot {sid}: motion must be one of {MOTIONS}")
    if "workflow" in sh and not (isinstance(sh["workflow"], str) and WORKFLOW_RE.match(sh["workflow"])):
        raise ValueError(f"shot {sid}: workflow must be a workflow name (lowercase letters, digits, - . _)")
    if "reference_video" in sh and not isinstance(sh["reference_video"], str):
        raise ValueError(f"shot {sid}: reference_video must be a path string")
    if "landing_pose" in sh and not isinstance(sh["landing_pose"], bool):
        raise ValueError(f"shot {sid}: landing_pose must be true or false")
    beats = sh.get("beats", [])
    if not isinstance(beats, list):
        raise ValueError(f"shot {sid}: beats must be a list")
    for b in beats:
        if not (isinstance(b, dict) and _num(b.get("t"), 0, 3600) and isinstance(b.get("action", ""), str)
                and isinstance(b.get("impact", False), bool)):
            raise ValueError(f"shot {sid}: each beat is {{t: seconds, action: text, impact: true/false}}")


def version_of(raw):
    return hashlib.sha1(raw).hexdigest()[:16]


def names(cfg):
    return sorted(p.parent.name for p in cfg.boards.glob("*/storyboard.json"))


def load(cfg, name, check=False):
    """check=True also validates (use it before acting on shot ids, e.g. frames and export)."""
    raw = cfg.board_file(name).read_bytes()
    b = json.loads(raw.decode("utf-8"))
    return (validate(b) if check else b), version_of(raw)


def current_version(cfg, name):
    return version_of(cfg.board_file(name).read_bytes())


def save(cfg, name, board):
    """Write atomically; the previous version goes to history/ (last HISTORY_KEEP kept). Returns the new version."""
    validate(board)
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
