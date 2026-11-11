"""storyboard: plan a video as shots on a visual board, with an AI agent drafting and revising it.

  storyboard serve [<name>] [--port 8770] [--no-open]   the boards in a browser
  storyboard new <name> [--kind story|music] [--title T] [--brief "director's notes"] [--draft]
                        [--size 1280x720] [--audio path --bpm 136 --first-beat 0.51]
  storyboard list <name> [--json]                       shot table (or the board JSON)
  storyboard requests [<name>] [--json]                 open requests from the board(s) for the agent
  storyboard done <name> [<shot_id>]                    clear a request once handled
  storyboard frames <name> [--shots A,B] [--style sketch|photo] [--missing] [--seed N] [--model ID] [--workflow api.json]
  storyboard models          frame models and whether each can run now
  storyboard export <name> [--approved | --shots A,B] [--dest DIR]
  storyboard rename <old> <new> | duplicate <name> <new> | delete <name>

Global options (or environment variables):
  --root   STORYBOARD_ROOT    base folder for file paths stored in boards (default: current folder)
  --boards STORYBOARD_BOARDS  board folders (default: <root>/storyboards; must be inside root)
  --comfy  COMFY_URL          ComfyUI server for `frames` (default http://127.0.0.1:8188)
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import board as bd
from . import frames as fr
from . import models as fm
from .server import serve


def cmd_serve(cfg, a):
    serve(cfg, a.port, a.name, not a.no_open, a.render_cmd)


def cmd_new(cfg, a):
    w, h = (int(v) for v in a.size.lower().split("x"))
    req = "Draft the shots from the director's notes." if a.draft else ""
    bd.create(cfg, a.name, a.title, a.kind, w, h, a.brief or "", req, a.audio, a.bpm, a.first_beat)
    print("created", cfg.board_file(a.name))


def cmd_list(cfg, a):
    b, ver = bd.load(cfg, a.name)
    if a.json:
        print(json.dumps(b, indent=1, ensure_ascii=False))
        return
    print(f"{b.get('title')}  ({b.get('kind')}, {len(b['shots'])} shots, version {ver})")
    for s, (t0, t1) in zip(b["shots"], bd.timings(b)):
        text = s.get("lyric") or s.get("dialogue") or s.get("action") or ""
        flags = ("S" if s.get("start_frame") else "-") + ("E" if s.get("end_frame") else "-") + ("A" if s.get("audio") else "-")
        req = "  REQUEST: " + s["request"] if s.get("request") else ""
        print(f"{s['id']:>6} {t0:7.2f}-{t1:7.2f} {float(s.get('duration', 0)):5.2f}s {s.get('status', 'draft'):8} "
              f"{flags} {(s.get('section') or ''):12.12} {text[:60]}{req}")


def cmd_requests(cfg, a):
    reqs = bd.requests(cfg, a.name)
    if a.json:
        print(json.dumps([{"board": n, "shot": s, "request": r} for n, s, r in reqs], indent=1, ensure_ascii=False))
        return
    if not reqs:
        print("no open requests")
    last = None
    for n, s, r in reqs:
        if n != last:
            b, _ = bd.load(cfg, n)
            print(f"== {n} ({b.get('title')})")
            if b.get("brief"):
                print("   brief:", b["brief"].replace("\n", " ")[:600])
            last = n
        print(f"   {s or 'BOARD'}: {r}")


def cmd_done(cfg, a):
    b, _ = bd.load(cfg, a.name)
    target = b if not a.shot else next((s for s in b["shots"] if s["id"] == a.shot), None)
    if target is None:
        sys.exit(f"no shot {a.shot}")
    target.pop("request", None)
    bd.save(cfg, a.name, b)
    print("cleared", a.name, a.shot or "board")


def cmd_frames(cfg, a):
    b, _ = bd.load(cfg, a.name, check=True)
    style = a.style or b.get("frame_style", "sketch")
    want = set(a.shots.split(",")) if a.shots else None
    template = json.loads(Path(a.workflow).read_text(encoding="utf-8")) if a.workflow else None
    models = {k: v for k, v in (("unet", a.unet), ("clip", a.clip), ("vae", a.vae)) if v}
    w, h = fr.frame_size(b, style)
    comfy = fr.Comfy(cfg.comfy_url)
    todo = [s for s in b["shots"] if (want is None or s["id"] in want) and not (a.missing and s.get("start_frame"))]
    reg = fm.registry()
    picks = {s["id"]: fm.shot_model(b, s, a.model) for s in todo} if not template else {}
    if picks:
        fm.Checker(cfg.comfy_url).require(sorted(set(picks.values())))
        paid = [k for k in picks.values() if reg[k].get("kind") == "cloud"]
        if paid:
            est = sum(reg[k].get("usd_per_image", 0) for k in paid)
            print(f"{len(paid)} frame(s) on paid models, estimated ${est:.2f}", flush=True)
    made = {}
    out_dir = cfg.board_dir(a.name) / "frames"
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, s in enumerate(todo):
        prompt = fr.frame_prompt(b, s, style)
        seed = a.seed if a.seed is not None else fr.shot_seed(s["id"])
        prefix = f"storyboards/{a.name}/{s['id']}"
        mid = picks.get(s["id"])
        entry = reg.get(mid, {})
        cost = 0
        if entry.get("kind") == "cloud":
            data, secs, cost = fm.openai_image(entry, prompt, w, h)
        else:
            t = template or fm.graph_template(entry)
            graph = (fr.template_graph(t, prompt, seed, w, h, prefix) if t
                     else fr.zimage_graph(prompt, seed, w, h, prefix, models))
            data, secs = comfy.run_image(graph)
        out = (out_dir / f"{s['id']}_{style}_{time.strftime('%Y%m%d_%H%M%S')}.png").resolve()
        if not out.is_relative_to(out_dir.resolve()):
            raise ValueError(f"shot id {s['id']!r} would write outside {out_dir}")
        out.write_bytes(data)
        out.with_suffix(".json").write_text(json.dumps({"prompt": prompt, "seed": seed, "size": [w, h],
                                                        "model": mid or a.workflow, "cost_usd": cost,
                                                        "seconds": round(secs, 1)}, indent=1), encoding="utf-8")
        made[s["id"]] = cfg.rel(out)
        print(f"[{i + 1}/{len(todo)}] {s['id']}: {out.name} ({mid or 'workflow'}, {secs:.0f}s"
              + (f", ${cost:.3f}" if cost else "") + ")", flush=True)
    b, _ = bd.load(cfg, a.name)  # re-read: the board may have been edited while frames rendered
    for s in b["shots"]:
        if s["id"] in made:
            s["start_frame"] = made[s["id"]]
            if s.get("status", "draft") == "draft":
                s["status"] = "frames"
    bd.save(cfg, a.name, b)
    print(len(made), "frame(s) set on the board")


def cmd_models(cfg, a):
    ms = fm.Checker(cfg.comfy_url).all()
    if a.json:
        print(json.dumps(ms, indent=1))
        return
    for m in ms:
        cost = f", ~${m['usd_per_image']:.2f}/frame" if m["usd_per_image"] else ""
        print(f"{m['id']:14} {m['kind']:5} {'yes' if m['available'] else 'no ':3}  {m['label']}{cost}"
              + (f"  ({m['reason']})" if m["reason"] else ""))


def cmd_export(cfg, a):
    """One folder per shot: start.png, end.png, prompt.txt, audio.wav (shot audio, or the board track cut to the
    shot's time), reference.<ext> (the shot's reference video, if any) and shot.json (timing, lane, motion profile,
    workflow, beats, landing_pose, character blocks, speaker voice id, packs). A neutral hand-off format for whatever
    renders the video."""
    b, _ = bd.load(cfg, a.name, check=True)
    dest = (Path(a.dest) if a.dest else cfg.board_dir(a.name) / "export").resolve()
    want = set(a.shots.split(",")) if a.shots else None
    track = (b.get("audio") or {}).get("file")
    n = 0
    for s, (t0, t1) in zip(b["shots"], bd.timings(b)):
        if want is not None and s["id"] not in want:
            continue
        if a.approved and s.get("status") not in ("approved", "rendered", "picked"):
            continue
        if not s.get("start_frame"):
            print("skip", s["id"], "(no start frame)")
            continue
        d = (dest / s["id"]).resolve()
        if d.parent != dest:
            raise ValueError(f"shot id {s['id']!r} would write outside {dest}")
        start = cfg.resolve(s["start_frame"])
        if not start or not start.is_file():
            print("skip", s["id"], "(start frame missing or outside the root)")
            continue
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(start, d / "start.png")
        end = cfg.resolve(s["end_frame"]) if s.get("end_frame") else None
        shutil.copy2(end if end and end.is_file() else start, d / "end.png")
        (d / "prompt.txt").write_text((s.get("motion_prompt") or s.get("action") or "") + "\n", encoding="utf-8")
        src = cfg.resolve(s.get("audio") or track) if (s.get("audio") or track) else None
        if src:
            if not shutil.which("ffmpeg"):
                sys.exit("ffmpeg is needed to export audio")
            cut = [] if s.get("audio") else ["-ss", f"{t0:.3f}", "-to", f"{t1:.3f}"]
            subprocess.run(["ffmpeg", "-v", "error", "-y", *cut, "-i", str(src), "-ar", "48000", "-ac", "2",
                            str(d / "audio.wav")], check=True)
        meta = {k: s.get(k) for k in ("id", "section", "duration", "dialogue", "speaker", "lyric", "camera",
                                      "characters", "backend", "seeds", "workflow", "beats",
                                      "frame_model") if s.get(k) not in (None, "", [])}
        meta.update(board=a.name, start=round(t0, 3), end=round(t1, 3), width=b.get("width"),
                    height=b.get("height"), fps=b.get("fps", 24), lane=s.get("lane", "A"), motion=bd.shot_motion(s),
                    landing_pose=bool(s.get("landing_pose", False)))
        ref = cfg.resolve(s["reference_video"]) if s.get("reference_video") else None
        if ref and ref.is_file():
            shutil.copy2(ref, d / ("reference" + ref.suffix.lower()))
            meta["reference_video"] = "reference" + ref.suffix.lower()
        chars = {c["id"]: c for c in b.get("characters", [])}
        blocks = {cid: chars[cid].get("block") or chars[cid].get("desc") for cid in s.get("characters", [])
                  if cid in chars and (chars[cid].get("block") or chars[cid].get("desc"))}
        if blocks:
            meta["character_blocks"] = blocks
        if s.get("speaker") in chars and chars[s["speaker"]].get("voice_id"):
            meta["voice_id"] = chars[s["speaker"]]["voice_id"]
        packs = {cid: chars[cid]["pack"] for cid in s.get("characters", []) if chars.get(cid, {}).get("pack")}
        if packs:
            meta["packs"] = packs
        (d / "shot.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        n += 1
        print("exported", s["id"], "->", d)
    print(n, "shot folder(s) in", dest)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="storyboard", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--boards")
    ap.add_argument("--comfy")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="open the boards in a browser")
    p.add_argument("name", nargs="?")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--no-open", action="store_true")
    p.add_argument("--render-cmd", help="command the board's Render button runs ({board} {shots} {seeds}); "
                                        "default: STORYBOARD_RENDER_CMD")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("new", help="create a board")
    p.add_argument("name")
    p.add_argument("--kind", choices=["story", "music"], default="story")
    p.add_argument("--title")
    p.add_argument("--brief", help="director's notes")
    p.add_argument("--draft", action="store_true", help="leave a request for the agent to draft shots from the brief")
    p.add_argument("--size", default="1280x720")
    p.add_argument("--audio", help="root-relative audio track (song or full voice-over)")
    p.add_argument("--bpm", type=float)
    p.add_argument("--first-beat", type=float, default=0.0)
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("list", help="show a board's shots")
    p.add_argument("name")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("requests", help="open requests for the agent")
    p.add_argument("name", nargs="?")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_requests)

    p = sub.add_parser("done", help="clear a handled request")
    p.add_argument("name")
    p.add_argument("shot", nargs="?")
    p.set_defaults(fn=cmd_done)

    p = sub.add_parser("frames", help="generate start frames with ComfyUI")
    p.add_argument("name")
    p.add_argument("--shots")
    p.add_argument("--style", choices=["sketch", "photo"])
    p.add_argument("--missing", action="store_true", help="only shots without a start frame")
    p.add_argument("--seed", type=int)
    p.add_argument("--model", help="frame model id for every shot (default: the shot's frame_model, then the board's)")
    p.add_argument("--workflow", help="API-format text-to-image workflow with {{prompt}} {{seed}} {{width}} {{height}}"
                                      " (overrides the frame models)")
    p.add_argument("--unet")
    p.add_argument("--clip")
    p.add_argument("--vae")
    p.set_defaults(fn=cmd_frames)

    p = sub.add_parser("models", help="frame models and whether each can run now")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_models)

    p = sub.add_parser("export", help="write shot folders for rendering")
    p.add_argument("name")
    p.add_argument("--shots")
    p.add_argument("--approved", action="store_true")
    p.add_argument("--dest")
    p.set_defaults(fn=cmd_export)

    for verb in ("rename", "duplicate"):
        p = sub.add_parser(verb)
        p.add_argument("name")
        p.add_argument("new")
        p.set_defaults(fn=lambda cfg, a: (bd.rename(cfg, a.name, a.new, copy=a.cmd == "duplicate"),
                                          print(a.cmd, a.name, "->", a.new)))
    p = sub.add_parser("delete", help="move a board to the trash folder")
    p.add_argument("name")
    p.set_defaults(fn=lambda cfg, a: print("moved to", bd.delete(cfg, a.name)))

    a = ap.parse_args(argv)
    cfg = bd.Config.from_env(a.root, a.boards, a.comfy)
    try:
        a.fn(cfg, a)
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
