# Working a storyboard as an agent

This file is for the AI agent (Claude Code, Codex, or any agent with a shell) that drafts and revises boards
together with a person. The person edits on the board in the browser; you edit the same `storyboard.json` from the
terminal. The board reloads by itself when the file changes, so your edits show up live.

## The loop

1. `storyboard requests` (or `--json`): lists open requests. A request is a board-level `request` (e.g. "Draft the
   shots from the director's notes.") or a shot-level `request` (e.g. "new frame, lower angle", "split in two").
   The board's `brief` (director's notes) is printed with it.
2. Do the work by editing `<boards>/<name>/storyboard.json` (see "Editing safely") and/or running commands
   (`storyboard frames ...`).
3. Clear each request you handled: `storyboard done <name> [<shot_id>]`, or delete the `request` key in your edit.
4. Tell the person what you changed in one or two lines. Leave anything you could not do in the request, reworded.

## Editing safely

- Load, change, save in one step, and re-read right before saving. The person may be editing at the same time.
  Saving through the package keeps a history copy and writes atomically:
  ```python
  from storyboard import board as bd
  cfg = bd.Config.from_env()            # uses STORYBOARD_ROOT / STORYBOARD_BOARDS
  b, _ = bd.load(cfg, "prison_escape")
  ...                                   # edit b
  bd.save(cfg, "prison_escape", b)
  ```
- If the person has unsaved edits when you save, their next save is refused (HTTP 409) and the board offers
  "Load latest", so neither side silently overwrites the other. Keep your edits small and focused for that reason.
- Every save keeps a copy in `<board>/history/`; nothing is lost.
- Keep unknown keys. Never rename `id`s of shots that have takes or frames unless asked.

## Board format (`storyboard.json`)

Board: `title`, `kind` (`story` | `music`), `width`, `height`, `fps`, `frame_style` (`sketch` | `photo`), `brief`
(director's notes), `request`, `look` (appended to photo frame prompts: film stock, light, palette), `notes`,
`audio` (`{file, offset, bpm, first_beat}`, optional board track), `sections` (`[{name, in, out}]`, music),
`characters` (`[{id, desc, voice_id}]`), `shots`.

Shot: `id`, `duration` (seconds; order = array order, start times are derived), `section` (scene), `location`,
`action` (what happens), `dialogue` + `speaker` (a character id) or `lyric`, `audio` (path), `cam`
(`{size, angle, move, lens, extra}`) and `camera` (the same as text: `size, angle, lens, move, extra`),
`characters` (ids), `start_frame`, `end_frame` (paths), `start_frame_prompt`, `end_frame_prompt`, `motion_prompt`,
`backend`, `seeds`, `notes`, `request`, `status` (`draft` → `frames` → `approved` → `rendered` → `picked`),
`takes` (`[{path, label}]`), `pick`.

Paths are relative to the root (`STORYBOARD_ROOT`). Board-owned files live in `<boards>/<name>/frames/` and `audio/`.

Camera values the board's dropdowns offer (free text is allowed too):
- size: Extreme wide, Wide, Full shot, Medium wide, Medium, Medium close-up, Close-up, Extreme close-up, Insert,
  Over-the-shoulder, Two-shot, Group shot, POV, Establishing
- angle: Eye level, Low angle, High angle, Overhead / top-down, Worm's eye, Dutch angle, Aerial / drone, Shoulder level
- move: Locked off, Handheld, Steadicam follow, Push-in, Pull-out, Pan left/right, Tilt up/down, Track left/right,
  Tracking alongside, Crane up/down, Orbit, Zoom in, Whip pan, Crash zoom
- lens: 14mm … 200mm, 100mm macro

## Drafting shots from a brief

- Read the brief for story beats, then cover each beat with 1-4 shots. Mix sizes (establish, then go closer),
  give every shot one clear action, and keep durations honest (2-5 s for most shots; inserts 1.5-2.5 s).
- Group shots into scenes with `section`; the timeline labels runs of the same section.
- Write `action` as what the camera sees, present tense, one or two sentences. Use character ids in `characters`
  and the character's name in the text; the frame generator swaps names for descriptions.
- Fill `cam` and set `camera` to the joined text. Add `dialogue` + `speaker` only where someone speaks.
- Add `characters` with a concrete visual `desc` (age, build, hair, clothes) so frames stay consistent.
- Put your interpretation (tone, what you added) in the board `notes`, so the person can push back.
- "Make variants" means new boards: `storyboard duplicate <name> <name>_b`, then rework the copy.

## Frames

- `storyboard frames <name> [--shots A,B] [--missing] [--style sketch|photo]` renders start frames through
  ComfyUI (default: Z-Image Turbo, ~4 s each on a 4090) and sets them on the board.
- Sketch for planning, photo for shots headed to video. A shot's `start_frame_prompt` overrides the generated
  prompt; use it when a frame needs specific direction.
- Image models write names and camera jargon into the picture as text. The generator keeps them out; if you write
  a `start_frame_prompt` yourself, describe people instead of naming them and leave out lens/move words.
- Only one heavy job on the GPU at a time: check that ComfyUI is idle before queueing frames.

## Hand-off

`storyboard export <name> --approved [--dest DIR]` writes one folder per approved shot (`start.png`, `end.png`,
`prompt.txt`, `audio.wav`, `shot.json`) for whatever video workflow renders it. Add finished renders back to a shot
as `takes` so the person can pick one on the board.
