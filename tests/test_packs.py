import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from storyboard import board as bd
from storyboard import frames as fr
from storyboard import packs as pk


class PackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = bd.Config.from_env(root=self.root, boards=self.root / "storyboards", comfy_url="http://127.0.0.1:1")
        d = self.root / "input" / "characters" / "vela"
        (d / "screens").mkdir(parents=True)
        for f in ("head.png", "front.png", "screens/outfit1.png", "screens/outfit2.png"):
            (d / f).write_bytes(b"png")
        (d / "block.txt").write_text("VELA BLOCK\n", encoding="utf-8")
        (d / "pack.json").write_text(json.dumps({
            "voice_id": "abc123", "prompt_block": "block.txt", "master_headshot": "head.png",
            "body_turnaround": {"front": "front.png"},
            "wardrobe": {"news_1": "screens/outfit1.png", "news_2": "screens/outfit2.png"}}), encoding="utf-8")
        self.d = d

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_block_refs(self):
        p = pk.load(self.cfg, "vela")
        self.assertEqual(pk.block(p), "VELA BLOCK")
        self.assertEqual([f.name for f in pk.refs(p)], ["head.png", "outfit1.png"])
        self.assertEqual([f.name for f in pk.refs(p, "news_2")], ["head.png", "outfit2.png"])
        self.assertIsNone(pk.load(self.cfg, "missing"))
        self.assertIsNone(pk.load(self.cfg, "../vela"))

    def test_front_view_when_no_wardrobe(self):
        j = json.loads((self.d / "pack.json").read_text(encoding="utf-8"))
        del j["wardrobe"]
        (self.d / "pack.json").write_text(json.dumps(j), encoding="utf-8")
        self.assertEqual([f.name for f in pk.refs(pk.load(self.cfg, "vela"))], ["head.png", "front.png"])

    def test_block_replaces_desc_in_photo_prompts_only(self):
        board = {"characters": [{"id": "VELA", "desc": "a reporter, blue skin", "pack": "vela"}]}
        shot = {"id": "S1", "action": "VELA talks to camera", "characters": ["VELA"]}
        self.assertEqual([c["id"] for c, _ in pk.shot_packs(self.cfg, board, shot)], ["VELA"])
        photo = fr.frame_prompt(board, shot, "photo", {"VELA": "VELA BLOCK"})
        self.assertIn("a reporter: VELA BLOCK", photo)
        self.assertNotIn("blue skin", photo)
        self.assertIn("blue skin", fr.frame_prompt(board, shot, "sketch", {"VELA": "VELA BLOCK"}))

    def test_ref_graph_sets_frame_size(self):
        tpl = {"l": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
               "r1": {"class_type": "LoadImage", "inputs": {"image": "{{image_1}}"}},
               "r2": {"class_type": "LoadImage", "inputs": {"image": "{{image_2}}"}},
               "e": {"class_type": "Enc", "inputs": {"prompt": "{{prompt}}", "a": ["r1", 0], "b": ["r2", 0]}}}
        g = fr.ref_graph(tpl, "p", 1, ["x.png"], 1344, 768, "pre")
        self.assertEqual((g["l"]["inputs"]["width"], g["l"]["inputs"]["height"]), (1344, 768))
        self.assertNotIn("r2", g)
        self.assertNotIn("b", g["e"]["inputs"])

    def test_export_fills_block_and_voice_from_pack(self):
        bd.create(self.cfg, "ex")
        d = self.cfg.board_dir("ex")
        (d / "frames").mkdir(exist_ok=True)
        (d / "frames" / "s.png").write_bytes(b"png")
        b, _ = bd.load(self.cfg, "ex")
        b["characters"] = [{"id": "VELA", "desc": "a reporter, blue skin", "pack": "vela"}]
        b["shots"] = [{"id": "S1", "duration": 2, "start_frame": "storyboards/ex/frames/s.png", "characters": ["VELA"],
                       "speaker": "VELA", "dialogue": "Hi."}]
        bd.save(self.cfg, "ex", b)
        r = subprocess.run([sys.executable, "-m", "storyboard", "--root", str(self.root), "--boards",
                            str(self.root / "storyboards"), "export", "ex"], capture_output=True, text=True,
                           cwd=Path(__file__).resolve().parent.parent)
        self.assertEqual(r.returncode, 0, r.stderr)
        m = json.loads((d / "export" / "S1" / "shot.json").read_text(encoding="utf-8"))
        self.assertEqual((m["character_blocks"], m["voice_id"]), ({"VELA": "VELA BLOCK"}, "abc123"))


if __name__ == "__main__":
    unittest.main()
