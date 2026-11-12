import json
import tempfile
import unittest
from pathlib import Path

from storyboard import board as bd
from storyboard import frames as fr
from storyboard import models as fm
from storyboard import revise as rv
from storyboard import server as sv
from storyboard.jobs import Jobs


class FramesPickerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = bd.Config.from_env(root=self.root, boards=self.root / "storyboards", comfy_url="http://127.0.0.1:1")
        d = self.root / "input" / "characters" / "vela"
        (d / "w").mkdir(parents=True)
        for f in ("head.png", "w/o1.png"):
            (d / f).write_bytes(b"png")
        (d / "pack.json").write_text(json.dumps({"display_name": "Vela", "voice_id": "abc", "master_headshot": "head.png",
                                                 "wardrobe": {"news_1": "w/o1.png"}}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_end_versions_keep_their_slot(self):
        s = {"id": "S1", "start_frame": "a.png", "status": "draft"}
        rv.add_versions(s, [{"path": "b.png"}])
        self.assertEqual((s["start_frame"], s["status"]), ("b.png", "frames"))
        rv.add_versions(s, [{"path": "e1.png", "slot": "end"}], "end")
        self.assertEqual((s["start_frame"], s["end_frame"]), ("b.png", "e1.png"))
        s["end_frame"] = "e0.png"
        rv.add_versions(s, [{"path": "e2.png", "slot": "end"}], "end")
        self.assertIn({"path": "e0.png", "slot": "end"}, s["versions"])
        self.assertEqual(s["end_frame"], "e2.png")

    def test_board_accepts_slot_and_rejects_bad_slot(self):
        bd.create(self.cfg, "b1")
        b, _ = bd.load(self.cfg, "b1")
        b["shots"] = [{"id": "S1", "duration": 2, "versions": [{"path": "x.png", "slot": "end"}]}]
        bd.save(self.cfg, "b1", b)
        b["shots"][0]["versions"][0]["slot"] = "middle"
        with self.assertRaises(ValueError):
            bd.save(self.cfg, "b1", b)

    def test_end_prompt_uses_last_beat_and_keeps_the_set(self):
        board = {"characters": [{"id": "RIO", "desc": "a man, black hoodie"}]}
        shot = {"id": "T6", "characters": ["RIO"], "action": "they fight",
                "beats": [{"t": 0, "action": "square off"}, {"t": 3, "action": "RIO holds his guard"}]}
        p = fr.end_prompt(board, shot)
        self.assertIn("a man holds his guard", p)
        self.assertIn("same people", p)
        self.assertEqual(fr.end_prompt(board, {**shot, "end_frame_prompt": "she lowers the cup"}).split(":")[1].strip()[:20],
                         "she lowers the cup. ")

    def test_end_model_needs_an_edit_model(self):
        self.assertEqual(fm.end_model({}, {"frame_model": "moss-dmd"}), fm.PACK_MODEL)
        self.assertEqual(fm.end_model({}, {"frame_model": "qwen21"}), "qwen21")
        self.assertEqual(fm.end_model({}, {"frame_model": "gpt-flare"}), "gpt-flare")

    def test_end_job_needs_a_start_frame(self):
        bd.create(self.cfg, "b2")
        b, _ = bd.load(self.cfg, "b2")
        b["shots"] = [{"id": "S1", "duration": 2}]
        bd.save(self.cfg, "b2", b)
        with self.assertRaises(ValueError):
            Jobs(self.cfg).submit("frames", "b2", ["S1"], slot="end")
        with self.assertRaises(ValueError):
            Jobs(self.cfg).submit("frames", "b2", ["S1"], slot="middle")

    def test_list_packs_and_images(self):
        packs = sv.list_packs(self.cfg)
        self.assertEqual([(p["name"], p["voice_id"], p["wardrobe"]) for p in packs], [("vela", "abc", ["news_1"])])
        self.assertEqual(packs[0]["headshot"], "input/characters/vela/head.png")
        bd.create(self.cfg, "b3")
        (self.cfg.board_dir("b3") / "frames").mkdir(exist_ok=True)
        (self.cfg.board_dir("b3") / "frames" / "S1_photo.png").write_bytes(b"png")
        (self.cfg.board_dir("b3") / "frames" / "S1_photo.json").write_text("{}", encoding="utf-8")
        self.assertEqual([i["path"] for i in sv.list_images(self.cfg, "b3", "board")], ["storyboards/b3/frames/S1_photo.png"])
        self.assertEqual({i["group"] for i in sv.list_images(self.cfg, "b3", "packs")}, {"vela"})
        self.assertEqual(len(sv.list_images(self.cfg, "b3", "boards")), 1)


if __name__ == "__main__":
    unittest.main()
