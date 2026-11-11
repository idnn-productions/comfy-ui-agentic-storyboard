"""Frame models: which image model makes a shot's start frame, and whether it can run right now.

A board has a default `frame_model`; any shot can set its own (for example ten cheap local frames to find the feel,
then the picked shots again on a cloud model). Built-in models are below. More can be added in a JSON file named by
STORYBOARD_MODELS ({id: entry}, same fields; a local `graph` path is relative to that file).

  local  an API-format ComfyUI graph with {{prompt}} {{seed}} {{width}} {{height}} placeholders. It is available when
         ComfyUI answers and has every model file the graph's loader nodes name (checked through /object_info).
  cloud  a paid image API. It is available when its key's environment variable is set (the key itself is never stored
         in a board or a file). Every frame on a cloud model costs money: the page shows the estimate before it runs,
         and the frame's .json records the measured cost.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

GRAPHS = Path(__file__).resolve().parent / "graphs"
DEFAULT = "zimage"

BUILTIN = {
    "zimage": {"label": "Z-Image Turbo", "kind": "local", "graph": "zimage",
               "note": "fast (a few seconds), planning sketches and rough photos"},
    "qwen21": {"label": "Qwen-Image 2.1", "kind": "local", "graph": "qwen21_t2i.api.json",
               "note": "local photo frames, 25 steps"},
    "moss": {"label": "MOSS-Image", "kind": "local", "graph": "moss_t2i.api.json",
             "note": "local, strongest at lettering (signs, posters)"},
    "gpt-flare": {"label": "GPT image 2.6 flare", "kind": "cloud", "provider": "openai", "model": "gpt-image-2.6-flare",
                  "env": "OPENAI_API_KEY", "usd_per_image": 0.05, "note": "paid, fast comps"},
    "gpt-sunburst": {"label": "GPT image 2.6 sunburst", "kind": "cloud", "provider": "openai",
                     "model": "gpt-image-2.6-sunburst", "env": "OPENAI_API_KEY", "usd_per_image": 0.05,
                     "note": "paid, final quality"},
}
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
            if v.get("kind") == "local" and v.get("graph") and v["graph"] != "zimage":
                v["graph"] = str(base / v["graph"])
            reg[k] = v
    return reg


def graph_template(entry):
    """The entry's API graph (None for the built-in Z-Image graph, which frames.zimage_graph builds)."""
    g = entry.get("graph")
    if g in (None, "zimage"):
        return None
    p = Path(g) if Path(g).is_absolute() else GRAPHS / g
    return json.loads(p.read_text(encoding="utf-8"))


def required_files(template):
    from .frames import ZIMAGE_MODELS
    if template is None:
        return {("UNETLoader", "unet_name"): ZIMAGE_MODELS["unet"], ("CLIPLoader", "clip_name"): ZIMAGE_MODELS["clip"],
                ("VAELoader", "vae_name"): ZIMAGE_MODELS["vae"]}
    out = {}
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
        try:
            files = required_files(graph_template(entry))
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
                        "available": ok, "reason": why})
        self._cache, self._at = out, time.time()
        return out

    def require(self, model_ids):
        """Raise ValueError naming the first model that is unknown or can't run."""
        by_id = {m["id"]: m for m in self.all()}
        for k in model_ids:
            if k not in by_id:
                raise ValueError(f"unknown frame model {k!r}")
            if not by_id[k]["available"]:
                raise ValueError(f"{by_id[k]['label']} is not available: {by_id[k]['reason']}")


def shot_model(board, shot, override=None):
    return override or shot.get("frame_model") or board.get("frame_model") or DEFAULT


def openai_image(entry, prompt, width, height, timeout=900):
    """Text-to-image on the OpenAI Images API. Returns (png bytes, seconds, cost_usd)."""
    key = os.environ.get(entry["env"])
    if not key:
        raise RuntimeError(f"{entry['env']} is not set")
    body = json.dumps({"model": entry["model"], "prompt": prompt, "size": f"{width}x{height}", "quality": "high",
                       "n": 1}).encode()
    req = urllib.request.Request("https://api.openai.com/v1/images/generations", data=body,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
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
