"""Frame generation through a running ComfyUI (HTTP API only: POST /prompt, GET /history, GET /view).

Default graph: Z-Image Turbo text-to-image (8 steps, res_multistep, CFG 1), model files configurable. Any other
text-to-image graph works too: export it from ComfyUI with "Export (API)" and put these placeholders in its widget
values: "{{prompt}}", "{{seed}}", "{{width}}", "{{height}}" (numbers may be written as the bare strings). The first
SaveImage output is used.
"""
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
import uuid

SKETCH = ("Rough storyboard sketch, black pencil and grey marker on white paper, loose confident lines, simple shading, "
          "one single hand-drawn film storyboard panel filling the image, not a comic page, no text. ")
NO_TEXT = " No words, no letters, no labels, no captions."

ZIMAGE_MODELS = {"unet": "z_image_turbo_bf16.safetensors", "clip": "qwen_3_4b.safetensors", "vae": "ae.safetensors"}


def _unname(text, board):
    for c in board.get("characters", []):
        short = (c.get("desc") or "").split(",")[0].strip() or "a person"
        for n in {c["id"], c["id"].capitalize(), c["id"].lower()}:
            text = re.sub(rf"\b{re.escape(n)}'s\b", short + "'s", text)
            text = re.sub(rf"\b{re.escape(n)}\b", short, text)
    return text


def frame_prompt(board, shot, style):
    """Image prompt for a shot's start frame.

    Names, ids and lens/move words are kept out on purpose: image models draw them as text (a "KADE" title, "Push in
    24mm" notes). Characters appear as their description, framing as shot size + angle. A start frame is one moment:
    when the shot has beats it shows the first beat (an action text like "stands, then drops" otherwise gets drawn as
    two figures), with the number of people stated. A shot's own start_frame_prompt wins over all of this.
    """
    if shot.get("start_frame_prompt"):
        base = shot["start_frame_prompt"]
    else:
        ids = shot.get("characters", [])
        chars = [c for c in board.get("characters", []) if c["id"] in ids]
        people = "; ".join(f"{(c.get('desc') or '').split(',')[0].strip() or 'a person'}: {c.get('desc')}"
                           for c in chars if c.get("desc") and "," in c["desc"])
        cam = shot.get("cam") or {}
        framing = " ".join(x for x in (cam.get("size"), cam.get("angle") and cam["angle"].lower()) if x)
        beats = shot.get("beats") or []
        if beats:
            where = shot.get("location") or ""
            count = {0: "No people.", 1: "Exactly one person in the frame."}.get(len(chars), f"Exactly {len(chars)} people in the frame.")
            moment = _unname(beats[0].get("action", ""), board)
            # the opening of the action text, cut before the sequence ("..., then suddenly drops"): one moment only
            setting = re.split(r",? (?:then|and then|before|until)\b", (shot.get("action") or "").split(".")[0])[0]
            setting = _unname(setting, board)
            base = (f"{framing} view. " if framing else "") + f"A single moment: {moment}. Setting: {setting}" +                    (f" ({where.lower()})" if where else "") + f". {count}" + (f" ({people})" if people else "")
        else:
            action = _unname(shot.get("action") or "", board)
            base = (f"{framing} view. " if framing else "") + action + (f" ({people})" if people else "")
    if style == "sketch":
        return SKETCH + base + NO_TEXT
    return f"{base} {board.get('look') or ''}".strip() + NO_TEXT


def frame_size(board, style):
    """~0.6 MP for sketches, ~1 MP for photos, keeping the board's aspect; multiples of 16."""
    w0, h0 = board.get("width", 1280), board.get("height", 720)
    area = 1024 * 576 if style == "sketch" else 1344 * 768
    s = (area / (w0 * h0)) ** 0.5
    return int(w0 * s) // 16 * 16, int(h0 * s) // 16 * 16


def shot_seed(shot_id):
    return int(hashlib.sha1(shot_id.encode()).hexdigest()[:6], 16)


def zimage_graph(prompt, seed, width, height, prefix, models=None):
    m = {**ZIMAGE_MODELS, **(models or {})}
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": m["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": m["clip"], "type": "lumina2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": m["vae"]}},
        "4": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.0}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "6": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["5", 0]}},
        "7": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "positive": ["5", 0], "negative": ["6", 0],
                                                   "latent_image": ["7", 0], "seed": seed, "steps": 8, "cfg": 1.0,
                                                   "sampler_name": "res_multistep", "scheduler": "simple", "denoise": 1.0}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": prefix}},
    }


def template_graph(template, prompt, seed, width, height, prefix):
    """Fill an API-format workflow's {{prompt}} / {{seed}} / {{width}} / {{height}} placeholders."""
    values = {"prompt": prompt, "seed": seed, "width": width, "height": height}

    def fill(v):
        if isinstance(v, str):
            m = re.fullmatch(r"\{\{(\w+)\}\}", v.strip())
            if m and m.group(1) in values:
                return values[m.group(1)]
            return re.sub(r"\{\{(\w+)\}\}", lambda x: str(values.get(x.group(1), x.group(0))), v)
        if isinstance(v, list):
            return [fill(x) for x in v]
        return v

    g = json.loads(json.dumps(template))
    for node in g.values():
        node["inputs"] = {k: fill(v) for k, v in node.get("inputs", {}).items()}
        if node.get("class_type") == "SaveImage":
            node["inputs"]["filename_prefix"] = prefix
    return g


class Comfy:
    def __init__(self, url, timeout=1800):
        self.url, self.timeout, self.client_id = url.rstrip("/"), timeout, uuid.uuid4().hex

    def _req(self, path, data=None):
        req = urllib.request.Request(self.url + path, data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Content-Type": "application/json"} if data is not None else {})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()

    def run_image(self, graph):
        """Queue a graph, wait for it, return the bytes of its first output image."""
        try:
            pid = json.loads(self._req("/prompt", {"prompt": graph, "client_id": self.client_id}))["prompt_id"]
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"ComfyUI rejected the graph: {e.read().decode(errors='replace')[:800]}") from e
        t0 = time.time()
        while time.time() - t0 < self.timeout:
            h = json.loads(self._req(f"/history/{pid}")).get(pid)
            if h:
                st = h.get("status", {})
                if st.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI error: {json.dumps(st.get('messages'))[:800]}")
                imgs = [i for o in h.get("outputs", {}).values() for i in o.get("images", []) if i.get("type") == "output"]
                if imgs:
                    q = urllib.parse.urlencode({k: imgs[0].get(k, "") for k in ("filename", "subfolder", "type")})
                    return self._req("/view?" + q), time.time() - t0
                if st.get("completed"):
                    raise RuntimeError("graph finished without an output image")
            time.sleep(0.5)
        raise RuntimeError("timed out waiting for ComfyUI")
