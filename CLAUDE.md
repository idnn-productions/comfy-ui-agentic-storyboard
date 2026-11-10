# CLAUDE.md

Read [AGENTS.md](AGENTS.md): it is the guide for working storyboard requests (the loop, the board format, drafting
shots, frames, hand-off).

Working on the code itself:
- Stdlib only (no dependencies). Python 3.10+. One page: `storyboard/web/board.html`, vanilla JS, no build step.
- `storyboard/board.py` storage, `server.py` HTTP API, `frames.py` ComfyUI client and prompts, `cli.py` commands.
- Keep the server's safety rules (see the module docstring in `server.py`): localhost Host check, `X-Storyboard`
  header on writes, file reads confined to the root, uploads confined to the board folder.
- The page must not use browser dialogs (`alert`, `confirm`, `prompt`); use the two-click confirm pattern.
- Tests: `python -m unittest discover -s tests`. LF line endings except `storyboard.cmd` (CRLF).
