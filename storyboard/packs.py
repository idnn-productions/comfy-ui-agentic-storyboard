"""Character packs: one folder per character with reference images, a prompt block and a voice id, indexed by pack.json.

A board character names its pack (`"pack": "vela_marr"`); packs live in STORYBOARD_PACKS, default <root>/input/characters.
pack.json keys used here (all paths relative to the pack folder; every key is optional):

  display_name, voice_id, prompt_block (a .txt file), master_headshot,
  body_turnaround / head_turnaround  {front, three_quarter, profile, three_quarter_back, back},
  expressions {name: file}, mouth_shapes {AH, OO, EE, FV, MBP}, details {name: file}, wardrobe {name: file}

What packs change:
  - photo frames: a character with a pack is described by its prompt block, and its master headshot and outfit (the
    shot's `outfit`, else the first wardrobe entry, else the front body view) go in as reference images. That needs a
    model that reads references (Qwen-Image 2.1 locally, GPT); a shot on a text-only model moves to Qwen-Image 2.1.
  - export: a character's missing block or voice id is filled from its pack.
"""
import json
import os
from pathlib import Path


def packs_dir(cfg):
    return Path(os.environ.get("STORYBOARD_PACKS") or cfg.root / "input" / "characters").resolve()


def load(cfg, name):
    """The pack's pack.json with a `dir` key added, or None if the pack folder or its pack.json is missing."""
    base = packs_dir(cfg)
    d = (base / name).resolve()
    if d.parent != base or not (d / "pack.json").is_file():
        return None
    p = json.loads((d / "pack.json").read_text(encoding="utf-8"))
    p["dir"] = d
    return p


def _file(pack, rel):
    if not rel:
        return None
    f = (pack["dir"] / rel).resolve()
    return f if f.is_relative_to(pack["dir"]) and f.is_file() else None


def block(pack):
    f = _file(pack, pack.get("prompt_block"))
    return f.read_text(encoding="utf-8").strip() if f else ""


def refs(pack, outfit=None):
    """[master headshot, outfit or full-body front] as existing files (either may be missing)."""
    ward = pack.get("wardrobe") or {}
    body = ward.get(outfit) if outfit else None
    body = body or next(iter(ward.values()), None) or (pack.get("body_turnaround") or {}).get("front")
    return [f for f in (_file(pack, pack.get("master_headshot")), _file(pack, body)) if f]


def shot_packs(cfg, board, shot):
    """[(character, pack)] for the shot's characters that name a pack that exists."""
    chars = {c["id"]: c for c in board.get("characters", [])}
    out = []
    for cid in shot.get("characters", []):
        c = chars.get(cid)
        p = load(cfg, c["pack"]) if c and c.get("pack") else None
        if p:
            out.append((c, p))
    return out
