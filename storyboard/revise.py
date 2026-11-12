"""Revise a frame from the director's annotations: numbered marks (boxes or strokes) with a note each, plus an overall
note, sent back to a frame model.

The page writes three things into the board's revisions/ folder: the request (.json), the frame with the marks drawn
on it (annotated .png) and a mask (.png, transparent where the marks are, soft edges). How each model uses them:

  edit (Qwen-Image 2.1, GPT)  reference images: the frame first, the annotated copy second, and an instruction built
                              from the notes ("1. top left (mark 1): ..."); GPT also gets the mask.
  inpaint (MOSS)              img2img on the frame: only the masked area is repainted (whole frame without marks), with
                              the frame's prompt plus the notes; `strength` is the denoise.

With marks and keep_rest (the default), the result is pasted back onto the original through the mask in ComfyUI, so
everything outside the marks stays pixel-identical. Each result becomes a new version of the shot's start frame; the
.json next to it records the parent frame, the notes, the model and the cost.
"""
import json
import struct
import time
from pathlib import Path

from . import board as bd
from . import frames as fr
from . import models as fm

MAX_MARKS = 12
MAX_TEXT = 600


def check_request(req):
    """Validate a revision request from the page; returns a clean copy (only known keys, bounded sizes)."""
    if not isinstance(req, dict):
        raise ValueError("revision must be an object")
    out = {"model": str(req.get("model") or ""), "note": str(req.get("note") or "")[:MAX_TEXT],
           "slot": "end" if req.get("slot") == "end" else "start",
           "count": int(req.get("count") or 1), "keep_rest": bool(req.get("keep_rest", True)),
           "strength": float(req.get("strength") or 0.9)}
    if not 1 <= out["count"] <= 10:
        raise ValueError("count must be 1-10")
    if not 0.2 <= out["strength"] <= 1.0:
        raise ValueError("strength must be 0.2-1.0")
    for k in ("source", "annotated", "mask"):
        v = req.get(k)
        if v is not None and not isinstance(v, str):
            raise ValueError(f"{k} must be a path")
        if v:
            out[k] = v
    if "source" not in out:
        raise ValueError("revision needs the source frame")
    marks = req.get("marks") or []
    if not isinstance(marks, list) or len(marks) > MAX_MARKS:
        raise ValueError(f"up to {MAX_MARKS} marks")
    out["marks"] = []
    for i, m in enumerate(marks, start=1):
        box = m.get("box") if isinstance(m, dict) else None
        if not (isinstance(box, list) and len(box) == 4 and all(isinstance(x, (int, float)) and 0 <= x <= 1 for x in box)):
            raise ValueError("each mark needs a box [x0, y0, x1, y1] in 0-1")
        out["marks"].append({"n": i, "text": str(m.get("text") or "")[:MAX_TEXT], "box": [round(x, 4) for x in box]})
    if not out["note"].strip() and not any(m["text"].strip() for m in out["marks"]):
        raise ValueError("write what to change (a note on a mark, or the overall note)")
    return out


def where(box):
    x, y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    v = "top" if y < 0.34 else "bottom" if y > 0.66 else "middle"
    h = "left" if x < 0.34 else "right" if x > 0.66 else "center"
    return "center" if (v, h) == ("middle", "center") else f"{v} {h}" if v != "middle" else f"middle {h}"


def changes(req, with_marks=True):
    lines = []
    for m in req["marks"]:
        if m["text"].strip():
            ref = f" (mark {m['n']})" if with_marks else ""
            lines.append(f"{m['n']}. In the {where(m['box'])} of the image{ref}: {m['text'].strip()}")
    if req["note"].strip():
        lines.append(("Overall: " if lines else "") + req["note"].strip())
    return lines


def edit_prompt(req, annotated):
    head = ("Edit the first image. The second image is the same picture with numbered red marks: they only show where "
            "each change goes and are not part of the picture." if annotated and req["marks"] else "Edit this image.")
    return " ".join([head, "Make these changes:", *changes(req, bool(annotated)),
                     "Keep everything else the same: the people, their faces, hair and clothes, the framing, the camera "
                     "angle, the light and the background. Do not draw the marks, numbers or any annotation text."])


def inpaint_prompt(req, board, shot):
    """Text-to-image models get the notes as the description of what goes in the marked areas, then the scene (shot
    characters, location, the board's look). Not the frame's own prompt: it still describes what is being replaced
    ("holding a coffee cup"), and the model paints that back (seen in testing, 2026-11-11)."""
    notes = [m["text"].strip().rstrip(".") for m in req["marks"] if m["text"].strip()]
    if req["note"].strip():
        notes.append(req["note"].strip().rstrip("."))
    chars = [c for c in board.get("characters", []) if c["id"] in (shot.get("characters") or [])]
    scene = [c.get("desc", "") for c in chars] + [shot.get("location") or "", board.get("look") or ""]
    return ". ".join([*notes, *(x.strip().rstrip(".") for x in scene if x.strip()), "Photograph, natural light"]) + "."


def png_size(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    return None


def _inside(cfg, name, rel):
    p = cfg.resolve(rel)
    if not p or not p.is_file() or not p.is_relative_to(cfg.board_dir(name).resolve()):
        raise ValueError(f"{rel} is not a file in the board's folder")
    return p


def run(cfg, name, shot_id, req, comfy, log=print):
    """Run a checked request; returns the new frame paths (root-relative). Updates the board."""
    b, _ = bd.load(cfg, name, check=True)
    shot = next((s for s in b["shots"] if s["id"] == shot_id), None)
    if shot is None:
        raise ValueError(f"no shot {shot_id}")
    reg = fm.registry()
    mid = req["model"] or fm.shot_model(b, shot)
    if mid not in reg:
        raise ValueError(fm.RETIRED.get(mid) or f"unknown frame model {mid!r}")
    entry = reg[mid]
    fm.Checker(cfg.comfy_url, ttl=0).require([mid])
    src_p = _inside(cfg, name, req["source"])
    src = src_p.read_bytes()
    ann = _inside(cfg, name, req["annotated"]).read_bytes() if req.get("annotated") else None
    mask = _inside(cfg, name, req["mask"]).read_bytes() if req.get("mask") and req["marks"] else None
    mode = entry.get("revise", "inpaint" if entry.get("kind") == "local" else "edit")
    meta_src = src_p.with_suffix(".json")
    meta = json.loads(meta_src.read_text(encoding="utf-8")) if meta_src.exists() else {}
    base_prompt = meta.get("base_prompt") or meta.get("prompt") or fr.frame_prompt(b, shot, b.get("frame_style", "sketch"))
    size = png_size(src) or fr.frame_size(b, b.get("frame_style", "sketch"))
    w, h = size[0] // 16 * 16, size[1] // 16 * 16
    prompt = edit_prompt(req, ann) if mode == "edit" else inpaint_prompt(req, b, shot)
    names = {}

    def up(key, data):  # upload to ComfyUI once
        if key not in names:
            names[key] = comfy.upload(data, f"{name}_{shot_id}_{key}_{int(time.time())}.png")
        return names[key]

    out_dir = cfg.board_dir(name) / "frames"
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    seed0 = fr.shot_seed(shot_id) + len(shot.get("versions") or []) + 1000
    for k in range(req["count"]):
        seed, cost, t0 = seed0 + k, 0.0, time.time()
        prefix = f"storyboards/{name}/{shot_id}_rev"
        if entry.get("kind") == "cloud":
            refs = [src] + ([ann] if ann and req["marks"] else [])
            data, _, cost = fm.openai_image(entry, prompt, w, h, refs=refs, mask=mask)
        elif mode == "edit":
            imgs = [up("src", src)] + ([up("ann", ann)] if ann and req["marks"] else [])
            data, _ = comfy.run_image(fr.edit_graph(fm.graph_template(entry, "edit_graph"), prompt, seed, imgs, prefix))
        else:
            strength = req["strength"] if req["marks"] else min(req["strength"], 0.65)
            g = fr.inpaint_graph(fm.graph_template(entry), prompt, seed, up("src", src),
                                 up("mask", mask) if mask else None, strength, prefix)
            data, _ = comfy.run_image(g)
        composited = False
        if mask and req["keep_rest"]:
            try:
                data, _ = comfy.run_image(fr.composite_graph(up("src", src), comfy.upload(data, f"{name}_{shot_id}_out.png"),
                                                             up("mask", mask), prefix + "_comp"))
                composited = True
            except (OSError, RuntimeError) as e:  # ComfyUI down: keep the model's full frame
                log(f"could not paste the edit back onto the original ({e}); keeping the full frame")
        out = (out_dir / f"{shot_id}_rev_{time.strftime('%Y%m%d_%H%M%S')}_{k}.png").resolve()
        if not out.is_relative_to(out_dir.resolve()):
            raise ValueError(f"shot id {shot_id!r} would write outside {out_dir}")
        out.write_bytes(data)
        secs = time.time() - t0
        out.with_suffix(".json").write_text(json.dumps({
            "prompt": prompt, "base_prompt": base_prompt, "seed": seed, "model": mid, "mode": mode,
            "from": req["source"], "marks": req["marks"], "note": req["note"], "keep_rest": composited,
            "strength": req["strength"] if mode == "inpaint" else None, "cost_usd": cost, "seconds": round(secs, 1)},
            indent=1), encoding="utf-8")
        made.append(cfg.rel(out))
        log(f"[{k + 1}/{req['count']}] {shot_id}: {out.name} ({mid} {mode}, {secs:.0f}s" + (f", ${cost:.3f}" if cost else "")
            + (", pasted onto the original" if composited else "") + ")")
    summary = "; ".join(changes(req, with_marks=False))[:200]
    b, _ = bd.load(cfg, name)  # re-read: the board may have been edited meanwhile
    for s in b["shots"]:
        if s["id"] == shot_id:
            slot = req.get("slot", "start")
            add_versions(s, [{"path": p, "model": mid, "from": req["source"], "note": summary,
                              **({"slot": "end"} if slot == "end" else {})} for p in made], slot)
    bd.save(cfg, name, b)
    return made


def add_versions(shot, items, slot="start"):
    """Append frame versions (newest last) and show the newest in the slot (start or end frame). The frame that was in
    the slot is kept as a version so it can be picked again. End-frame versions carry "slot": "end"."""
    key = "end_frame" if slot == "end" else "start_frame"
    v = shot.setdefault("versions", [])
    if shot.get(key) and not any(x.get("path") == shot[key] for x in v):
        v.append({"path": shot[key], **({"slot": "end"} if slot == "end" else {})})
    v.extend(items)
    if items:
        shot[key] = items[-1]["path"]
        if slot != "end" and shot.get("status", "draft") == "draft":
            shot["status"] = "frames"
