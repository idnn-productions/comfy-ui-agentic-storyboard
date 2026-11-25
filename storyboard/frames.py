"""Frame generation through a running ComfyUI (HTTP API only: POST /prompt, GET /history, GET /view).

The graphs come from the frame models (models.py). Any other text-to-image graph works too: export it from ComfyUI
with "Export (API)" and put these placeholders in its widget values: "{{prompt}}", "{{seed}}", "{{width}}",
"{{height}}" (numbers may be written as the bare strings). The first saved output image is used.
"""
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SKETCH = ("Rough storyboard sketch, black pencil and grey marker on white paper, loose confident lines, simple shading, "
          "one single hand-drawn film storyboard panel filling the image, not a comic page, no text. ")
NO_TEXT = " No words, no letters, no labels, no captions."


def _unname(text, board):
    # one pass per character, so a description that contains the name ("a prospector, ...") is not replaced again
    for c in board.get("characters", []):
        short = (c.get("desc") or "").split(",")[0].strip() or "a person"
        # the full name first ("Vela Marr"), so its first word is not swapped on its own and the surname left over
        full = {c["name"]} if isinstance(c.get("name"), str) and c["name"].strip() else set()
        names = "|".join(re.escape(n) for n in sorted({c["id"], c["id"].capitalize(), c["id"].lower()} | full,
                                                       key=len, reverse=True))
        text = re.sub(rf"\b(?:{names})\b", lambda m: short, text)
    return text


def traits_line(board, shot):
    """Locked character traits for the characters in this shot, as one sentence ("" when none).

    A character's `traits` holds what a model keeps getting wrong and must never change (Vela's eyes came out brown on
    SH02 and again on a thumbnail: "pale green-grey eyes, not brown"). It is added to every photo frame prompt of a shot
    that lists the character, including shots with their own start/end frame prompt, so no path can drop it."""
    ids = shot.get("characters", [])
    parts = [_labeled((c.get("desc") or "").split(",")[0].strip() or "a person", c["traits"].strip())
             for c in board.get("characters", [])
             if c["id"] in ids and isinstance(c.get("traits"), str) and c["traits"].strip()]
    return f" Always: {'; '.join(parts)}." if parts else ""


def _labeled(label, text):
    """"label: text", or just the text when it already starts with the label (no "a man: a man, ...")."""
    return text if text.lower().startswith(label.lower()) else f"{label}: {text}"


def frame_prompt(board, shot, style, blocks=None):
    """Image prompt for a shot's start frame.

    Names, ids and lens/move words are kept out on purpose: image models draw them as text (a "KADE" title, "Push in
    24mm" notes). Characters appear as their description, framing as shot size + angle. A start frame is one moment:
    when the shot has beats it shows the first beat (an action text like "stands, then drops" otherwise gets drawn as
    two figures), with the number of people stated. A shot's own start_frame_prompt wins over all of this.
    `blocks` ({character id: text}, from character packs) replaces those characters' descriptions in photo frames.
    """
    if shot.get("start_frame_prompt"):
        base = _unname(shot["start_frame_prompt"], board)
    else:
        ids = shot.get("characters", [])
        chars = [c for c in board.get("characters", []) if c["id"] in ids]
        blocks = {k: _unname(v, board) for k, v in (blocks or {}).items()} if style != "sketch" else None
        people = "; ".join(_labeled((c.get('desc') or '').split(',')[0].strip() or 'a person',
                                    (blocks or {}).get(c['id']) or c.get('desc'))
                           for c in chars if (blocks or {}).get(c["id"]) or (c.get("desc") and "," in c["desc"]))
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
    look = shot["look"] if isinstance(shot.get("look"), str) else board.get("look") or ""  # a shot's "" turns it off
    return f"{base}{traits_line(board, shot)} {look}".strip() + NO_TEXT


END_KEEP = ("Keep the same people, faces, hair, clothes, lighting, lens, camera position and setting as the first "
            "reference image; change only the pose, position and expression this moment needs.")
END_KEEP_MOVING = ("Keep the same people, faces, hair, clothes, lighting, lens and setting as the first reference image; "
                   "the camera has moved as described, and only that and the pose, position and expression this moment "
                   "needs change.")
STILL_MOVES = ("", "locked off", "handheld", "static")


def end_prompt(board, shot, blocks=None):
    """Prompt for a shot's end frame, made as an edit of its start frame (the first reference image): the shot's own
    end_frame_prompt, else its last beat, else its action, as the moment at the end of the shot."""
    if shot.get("end_frame_prompt"):
        moment = _unname(shot["end_frame_prompt"], board)
    else:
        beats = shot.get("beats") or []
        moment = _unname((beats[-1].get("action") if beats else None) or shot.get("action") or "", board)
    ids = shot.get("characters", [])
    who = "; ".join(_unname(b, board) for cid, b in (blocks or {}).items() if cid in ids)
    move = ((shot.get("cam") or {}).get("move") or "").strip().lower()
    keep = END_KEEP if move in STILL_MOVES else END_KEEP_MOVING
    return (f"The same shot a moment later, at the end of the shot: {moment}. {keep}"
            + (f" ({who})" if who else "") + traits_line(board, shot) + NO_TEXT)


def frame_size(board, style):
    """~0.6 MP for sketches, ~1 MP for photos, keeping the board's aspect; multiples of 16."""
    w0, h0 = board.get("width", 1280), board.get("height", 720)
    area = 1024 * 576 if style == "sketch" else 1344 * 768
    s = (area / (w0 * h0)) ** 0.5
    return int(w0 * s) // 16 * 16, int(h0 * s) // 16 * 16


def shot_seed(shot_id):
    return int(hashlib.sha1(shot_id.encode()).hexdigest()[:6], 16)


SAVERS = ("SaveImage", "SaveImageAdvanced")


def template_graph(template, prompt, seed, width, height, prefix, **extra):
    """Fill an API-format workflow's {{prompt}} / {{seed}} / {{width}} / {{height}} placeholders (and any `extra` ones,
    such as {{image_1}} in an edit graph)."""
    values = {"prompt": prompt, "seed": seed, "width": width, "height": height, **extra}

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
        if node.get("class_type") in SAVERS:
            node["inputs"]["filename_prefix"] = prefix
    return g


def edit_graph(template, prompt, seed, images, prefix):
    """An edit graph with {{image_1}}..{{image_N}} LoadImage nodes: fill the given ComfyUI image names and drop the
    LoadImage nodes (and the encoder inputs that use them) for images that were not given."""
    g = template_graph(template, prompt, seed, 0, 0, prefix, **{f"image_{i + 1}": n for i, n in enumerate(images)})
    unused = [k for k, n in g.items() if n.get("class_type") == "LoadImage"
              and isinstance(n["inputs"].get("image"), str) and n["inputs"]["image"].startswith("{{")]
    for k in unused:
        g.pop(k)
        for n in g.values():
            for name in [i for i, v in n["inputs"].items() if isinstance(v, list) and v and v[0] == k]:
                del n["inputs"][name]
    return g


MAX_REFS = 16  # TextEncodeQwenImage21 takes images.image_1 .. image_16


def more_ref_slots(template, n):
    """The edit graph with LoadImage slots {{image_1}}..{{image_n}}: the template has two; extra ones are wired into
    the encoder that reads images.image_1 (Qwen-Image 2.1). Templates without such an encoder are returned as is."""
    g = json.loads(json.dumps(template))
    enc = next((k for k, v in g.items() if "images.image_1" in v.get("inputs", {})), None)
    if not enc:
        return g
    for i in range(1, min(n, MAX_REFS) + 1):
        if f"images.image_{i}" not in g[enc]["inputs"]:
            g[f"ref{i}"] = {"class_type": "LoadImage", "inputs": {"image": f"{{{{image_{i}}}}}"}}
            g[enc]["inputs"][f"images.image_{i}"] = [f"ref{i}", 0]
    return g


def ref_graph(template, prompt, seed, images, width, height, prefix):
    """A new frame from reference images (character packs, set and prop refs): the edit graph, with its empty latent
    set to the frame size instead of the edit default (the size of the first reference image). In the Qwen-Image 2.1
    edit graph a ComfySwitchNode picks the latent: on_false = the encoder's latent at the reference's size, on_true =
    the empty latent; it is switched to the empty latent here."""
    g = edit_graph(more_ref_slots(template, len(images)), prompt, seed, images, prefix)
    empties = set()
    for k, n in g.items():
        if n.get("class_type", "").startswith("Empty") and "Latent" in n["class_type"]:
            n["inputs"]["width"], n["inputs"]["height"] = width, height
            empties.add(k)
    for n in g.values():
        if n.get("class_type") == "ComfySwitchNode":
            src = n["inputs"].get("on_true")
            if isinstance(src, list) and src and src[0] in empties:
                n["inputs"]["switch"] = True
    return g


def inpaint_graph(template, prompt, seed, image, mask, denoise, prefix):
    """Turn a text-to-image graph into img2img: the empty latent becomes the encoded `image` (a ComfyUI image name),
    masked by `mask` when given (an image whose transparent pixels mark the area to repaint; LoadImage turns
    transparency into mask = 1), and the sampler starts at `denoise`."""
    g = template_graph(template, prompt, seed, 0, 0, prefix)
    empty = next((k for k, n in g.items() if n["class_type"].startswith("Empty") and "Latent" in n["class_type"]), None)
    vae = next((k for k, n in g.items() if n["class_type"] == "VAELoader"), None)
    samplers = [n for n in g.values() if n["class_type"] in ("KSampler", "KSamplerAdvanced")]
    if not (empty and vae and samplers):
        raise ValueError("this graph cannot inpaint (needs an Empty...Latent node, a VAELoader and a KSampler)")
    g["src"] = {"class_type": "LoadImage", "inputs": {"image": image}}
    latent = ["enc", 0]
    g["enc"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["src", 0], "vae": [vae, 0]}}
    if mask:
        g["msk"] = {"class_type": "LoadImage", "inputs": {"image": mask}}
        g["noise_mask"] = {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["enc", 0], "mask": ["msk", 1]}}
        latent = ["noise_mask", 0]
    for n in g.values():
        for k, v in n["inputs"].items():
            if isinstance(v, list) and v and v[0] == empty:
                n["inputs"][k] = latent
    g.pop(empty)
    for n in samplers:
        n["inputs"]["denoise"] = denoise
    return g


def composite_graph(original, edited, mask, prefix):
    """Paste `edited` onto `original` only where `mask` is transparent (soft edges kept), so the rest of the frame
    stays pixel-identical. All three are ComfyUI image names; the edit is scaled to the original's size."""
    return {
        "o": {"class_type": "LoadImage", "inputs": {"image": original}},
        "e": {"class_type": "LoadImage", "inputs": {"image": edited}},
        "m": {"class_type": "LoadImage", "inputs": {"image": mask}},
        "c": {"class_type": "ImageCompositeMasked", "inputs": {"destination": ["o", 0], "source": ["e", 0], "x": 0, "y": 0,
                                                             "resize_source": True, "mask": ["m", 1]}},
        "s": {"class_type": "SaveImage", "inputs": {"images": ["c", 0], "filename_prefix": prefix}},
    }


class Comfy:
    def __init__(self, url, timeout=1800):
        self.url, self.timeout, self.client_id = url.rstrip("/"), timeout, uuid.uuid4().hex

    def _req(self, path, data=None):
        req = urllib.request.Request(self.url + path, data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Content-Type": "application/json"} if data is not None else {})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()

    def upload(self, data, name):
        """Put image bytes into ComfyUI's input folder (subfolder storyboard/); returns the LoadImage name."""
        b = uuid.uuid4().hex
        body = (f'--{b}\r\nContent-Disposition: form-data; name="subfolder"\r\n\r\nstoryboard\r\n'
                f'--{b}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'
                f'--{b}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
                f"Content-Type: image/png\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
        req = urllib.request.Request(self.url + "/upload/image", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={b}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read())
        return f"{d['subfolder']}/{d['name']}" if d.get("subfolder") else d["name"]

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
