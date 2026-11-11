import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from storyboard import board as bd
from storyboard import models as fm
from storyboard.jobs import Jobs


class FakeChecker(fm.Checker):
    """ComfyUI that has the given model files."""

    def __init__(self, files):
        super().__init__("http://127.0.0.1:1", ttl=0)
        self.files = set(files)

    def _choices(self, cls, key, info):
        return self.files


class ModelTests(unittest.TestCase):
    def test_shot_then_board_then_default(self):
        self.assertEqual(fm.shot_model({}, {}), "zimage")
        self.assertEqual(fm.shot_model({"frame_model": "qwen21"}, {}), "qwen21")
        self.assertEqual(fm.shot_model({"frame_model": "qwen21"}, {"frame_model": "gpt-flare"}), "gpt-flare")
        self.assertEqual(fm.shot_model({"frame_model": "qwen21"}, {"frame_model": "gpt-flare"}, "moss"), "moss")

    def test_local_needs_every_graph_file(self):
        files = fm.required_files(fm.graph_template(fm.BUILTIN["qwen21"])).values()
        self.assertIn("qwen_image_2.1_int8_convrot.safetensors", files)
        ok = {m["id"]: m for m in FakeChecker(files).all()}
        self.assertTrue(ok["qwen21"]["available"])
        self.assertFalse(ok["moss"]["available"])
        self.assertIn("moss_image_sft_int8_convrot.safetensors", ok["moss"]["reason"])

    def test_unreachable_comfy_makes_local_unavailable(self):
        ms = {m["id"]: m for m in fm.Checker("http://127.0.0.1:1", ttl=0).all()}
        self.assertFalse(ms["zimage"]["available"])
        self.assertEqual(ms["zimage"]["reason"], "ComfyUI is not reachable")

    def test_cloud_needs_its_key_and_never_reports_it(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            m = {x["id"]: x for x in FakeChecker([]).all()}["gpt-flare"]
            self.assertFalse(m["available"])
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-secret"}):
            ms = FakeChecker([]).all()
            self.assertTrue({x["id"]: x for x in ms}["gpt-flare"]["available"])
            self.assertNotIn("sk-test-secret", json.dumps(ms))

    def test_extra_models_file(self):
        with tempfile.TemporaryDirectory() as d:
            g = Path(d) / "mine.api.json"
            g.write_text(json.dumps({"1": {"class_type": "UNETLoader", "inputs": {"unet_name": "mine.safetensors"}}}))
            reg = Path(d) / "models.json"
            reg.write_text(json.dumps({"mine": {"label": "Mine", "kind": "local", "graph": "mine.api.json"}}))
            with mock.patch.dict(os.environ, {"STORYBOARD_MODELS": str(reg)}):
                ms = {m["id"]: m for m in FakeChecker(["mine.safetensors"]).all()}
            self.assertTrue(ms["mine"]["available"])

    def test_frames_job_refused_when_model_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = bd.Config.from_env(root=Path(d), boards=Path(d) / "storyboards", comfy_url="http://127.0.0.1:1")
            bd.create(cfg, "demo")
            b, _ = bd.load(cfg, "demo")
            b["shots"] = [{"id": "SH01", "duration": 2, "frame_model": "moss"}]
            bd.save(cfg, "demo", b)
            jobs = Jobs(cfg)
            jobs.models = FakeChecker(["z_image_turbo_bf16.safetensors"])
            with self.assertRaisesRegex(ValueError, "MOSS-Image is not available"):
                jobs.submit("frames", "demo", ["SH01"])
            with self.assertRaisesRegex(ValueError, "unknown frame model"):
                b["shots"][0]["frame_model"] = "nope"
                bd.save(cfg, "demo", b)
                jobs.submit("frames", "demo", ["SH01"])


if __name__ == "__main__":
    unittest.main()
