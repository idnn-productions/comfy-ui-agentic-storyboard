"""Frame models: which image model makes a shot's start frame, and whether it can run right now.

A board has a default `frame_model`; any shot can set its own. The models form a ladder from cheap to expensive
(LADDER): start on the local MOSS and Qwen models to find the feel, move a shot up to GPT flare when the local frames
are not good enough, and to sunburst last. Built-in models are below. More can be added in a JSON file named by
STORYBOARD_MODELS ({id: entry}, same fields; a local `graph` path is relative to that file).

  local  an API-format ComfyUI graph with {{prompt}} {{seed}} {{width}} {{height}} placeholders. It is available when
         ComfyUI answers and has every model file the graph's loader nodes name (checked through /object_info).
  cloud  a paid image API. It is available when its key's environment variable is set (the key itself is never stored
         in a board or a file). Every frame on a cloud model costs money: the page shows the estimate before it runs,
         and the frame's .json records the measured cost.

Revising a frame (annotate it, send it back) works on every model, in one of two ways (`revise`):
  edit     the model reads the frame (and the annotated copy) as reference images: Qwen-Image 2.1 (`edit_graph`), GPT.
  inpaint  text-to-image models repaint the marked areas of the frame (img2img with a latent noise mask): MOSS.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

GRAPHS = Path(__file__).resolve().parent / "graphs"
DEFAULT = "moss-dmd"
PACK_MODEL = "qwen21"  # local model for frames with character pack references
RETIRED = {"zimage": "Z-Image Turbo was removed (2026-11-11): its frames were not usable"}

BUILTIN = {
    "moss-dmd": {"label": "MOSS-Image 8-step DMD", "kind": "local", "graph": "moss_dmd_t2i.api.json", "revise": "inpaint",
                 "note": "local, ~3 s; first passes (8-step DMD merged into the int8 weights)"},
    "moss": {"label": "MOSS-Image", "kind": "local", "graph": "moss_t2i.api.json", "revise": "inpaint",
             "note": "local, ~15 s; strongest local model at lettering (signs, posters)"},
    "qwen21": {"label": "Qwen-Image 2.1", "kind": "local", "graph": "qwen21_t2i.api.json", "revise": "edit",
               "edit_graph": "qwen21_edit.api.json",
               "note": "local, ~10 s; best identity from references, follows edit instructions loosely"},
    "gpt-flare": {"label": "GPT image 2.6 flare", "kind": "cloud", "provider": "openai", "model": "gpt-image-2.6-flare",
                  "env": "OPENAI_API_KEY", "usd_per_image": 0.05, "revise": "edit",
                  "note": "paid, ~12 s; when the local frames are not good enough"},
    "gpt-sunburst": {"label": "GPT image 2.6 sunburst", "kind": "cloud", "provider": "openai",
                     "model": "gpt-image-2.6-sunburst", "env": "OPENAI_API_KEY", "usd_per_image": 0.05, "revise": "edit",
                     "note": "paid, ~40 s; last step when flare is not good enough"},
}
# cheap to expensive: the page's "next model up" button follows this order
LADDER = ["moss-dmd", "moss", "qwen21", "gpt-flare", "gpt-sunburst"]
# OpenAI image pricing per token (input text, input image, output image), USD; output tokens dominate.
OPENAI_RATES = (5e-6, 8e-6, 30e-6)

# loader class -> input that names a model file
LOADERS = {"UNETLoader": "unet_name", "CLIPLoader": "clip_name", "VAELoader": "vae_name",
           "CheckpointLoaderSimple": "ckpt_name", "LoraLoader": "lora_name", "LoraLoaderModelOnly": "lora_name",
           "DualCLIPLoader": ("clip_name1", "clip_name2")}


def registry():
    reg = {k: dict(v) for k, v in BUILTIN.items()}
    extra = os.environ.get("STORYBOARD_MODELS")
    if extra and Path(extra).exists():
        base = Path(extra).resolve().parent
        for k, v in json.loads(Path(extra).read_text(encoding="utf-8")).items():
            v = dict(v)
            for g in ("graph", "edit_graph"):
                if v.get("kind") == "local" and v.get(g):
                    v[g] = str(base / v[g])
            reg[k] = v
    return reg


def graph_template(entry, key="graph"):
    """The entry's API graph (`graph` for text-to-image, `edit_graph` for reference edits); None if it has none."""
    g = entry.get(key)
    if not g:
        return None
    p = Path(g) if Path(g).is_absolute() else GRAPHS / g
    return json.loads(p.read_text(encoding="utf-8"))


def required_files(template):
    out = {}
    if template is None:
        return out
    for node in template.values():
        keys = LOADERS.get(node.get("class_type"))
        for k in ([keys] if isinstance(keys, str) else keys or []):
            v = node["inputs"].get(k)
            if isinstance(v, str):
                out[(node["class_type"], k, v)] = v
    return out


class Checker:
    """Availability of every model, cached for `ttl` seconds (the page asks often)."""

    def __init__(self, comfy_url, ttl=30):
        self.comfy_url, self.ttl, self._at, self._cache = comfy_url.rstrip("/"), ttl, 0, None

    def _choices(self, cls, key, info):
        if cls not in info:
            with urllib.request.urlopen(f"{self.comfy_url}/object_info/{cls}", timeout=10) as r:
                info[cls] = json.loads(r.read()).get(cls, {})
        spec = info[cls].get("input", {}).get("required", {}).get(key)
        return set(spec[0]) if spec and isinstance(spec[0], list) else set()

    def check(self, model_id, entry, info):
        if entry.get("kind") == "cloud":
            env = entry.get("env", "")
            return (True, "") if env and os.environ.get(env) else (False, f"set {env} to use it (paid)")
        if not entry.get("graph"):
            return False, "no graph"
        try:
            files = {**required_files(graph_template(entry)), **required_files(graph_template(entry, "edit_graph"))}
        except (OSError, ValueError) as e:
            return False, f"graph unreadable: {e}"
        try:
            missing = [f for (cls, key, *_), f in files.items() if f not in self._choices(cls, key, info)]
        except (urllib.error.URLError, OSError, ValueError):
            return False, "ComfyUI is not reachable"
        return (False, "missing in ComfyUI: " + ", ".join(missing)) if missing else (True, "")

    def all(self):
        if self._cache is not None and time.time() - self._at < self.ttl:
            return self._cache
        info, out = {}, []
        for k, v in registry().items():
            ok, why = self.check(k, v, info)
            out.append({"id": k, "label": v.get("label", k), "kind": v.get("kind"), "note": v.get("note", ""),
                        "usd_per_image": v.get("usd_per_image", 0) if v.get("kind") == "cloud" else 0,
                        "revise": v.get("revise", "inpaint" if v.get("kind") == "local" else "edit"),
                        "rank": LADDER.index(k) if k in LADDER else len(LADDER),
                        "available": ok, "reason": why})
        out.sort(key=lambda m: m["rank"])
        self._cache, self._at = out, time.time()
        return out

    def require(self, model_ids):
        """Raise ValueError naming the first model that is unknown or can't run."""
        by_id = {m["id"]: m for m in self.all()}
        for k in model_ids:
            if k not in by_id:
                raise ValueError(RETIRED.get(k) or f"unknown frame model {k!r}")
            if not by_id[k]["available"]:
                raise ValueError(f"{by_id[k]['label']} is not available: {by_id[k]['reason']}")


def can_edit(entry):
    """True when the model reads reference images (a cloud model, or a local one with an edit graph)."""
    return entry.get("kind") == "cloud" or bool(entry.get("edit_graph"))


def end_model(board, shot, override=None):
    """End frames are edits of the start frame: the shot's model if it can edit, else PACK_MODEL."""
    m = shot_model(board, shot, override)
    return m if can_edit(registry().get(m, {})) else PACK_MODEL


def shot_model(board, shot, override=None):
    """override, then the shot's model, then the board's, then DEFAULT; a retired model falls through to the next."""
    for m in (override, shot.get("frame_model"), board.get("frame_model")):
        if m and m not in RETIRED:
            return m
    return DEFAULT


def next_up(model_id):
    """The next model on the ladder (None at the top or for a model that is not on it)."""
    i = LADDER.index(model_id) if model_id in LADDER else -1
    return LADDER[i + 1] if 0 <= i < len(LADDER) - 1 else None


def _multipart(fields, files):
    """multipart/form-data body (stdlib only). files: [(field, filename, bytes, content type)]."""
    b = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, name, data, ctype in files:
        out.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\n'
                   f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n")
    out.append(f"--{b}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={b}"


def openai_image(entry, prompt, width, height, refs=(), mask=None, timeout=900):
    """OpenAI Images API: text-to-image, or an edit when `refs` (PNG bytes, the edited image first) are given; `mask`
    (PNG bytes, transparent where the image may change) applies to the first ref. Returns (png, seconds, cost_usd)."""
    key = os.environ.get(entry["env"])
    if not key:
        raise RuntimeError(f"{entry['env']} is not set")
    fields = {"model": entry["model"], "prompt": prompt, "size": f"{width}x{height}", "quality": "high", "n": 1}
    head = {"Authorization": f"Bearer {key}"}
    if refs:
        files = [("image[]", f"ref{i}.png", r, "image/png") for i, r in enumerate(refs)]
        if mask:
            files.append(("mask", "mask.png", mask, "image/png"))
        body, ctype = _multipart(fields, files)
        url = "https://api.openai.com/v1/images/edits"
    else:
        body, ctype = json.dumps(fields).encode(), "application/json"
        url = "https://api.openai.com/v1/images/generations"
    req = urllib.request.Request(url, data=body, headers={**head, "Content-Type": ctype})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OpenAI rejected the request ({e.code}): {e.read().decode(errors='replace')[:400]}") from e
    u = d.get("usage", {})
    det = u.get("input_tokens_details", {})
    t, i, o = OPENAI_RATES
    cost = det.get("text_tokens", 0) * t + det.get("image_tokens", 0) * i + u.get("output_tokens", 0) * o
    return base64.b64decode(d["data"][0]["b64_json"]), time.time() - t0, round(cost, 4)
