import json
import tempfile
import unittest
from pathlib import Path

from storyboard import board as bd
from storyboard import frames as fr
from storyboard import models as fm
from storyboard import revise as rv
from storyboard.jobs import Jobs

from test_models import FakeChecker


def req(**kw):
    r = {"source": "storyboards/demo/frames/SH01.png", "model": "moss", "note": "",
         "marks": [{"text": "make the cup a microphone", "box": [0.1, 0.1, 0.3, 0.4]}]}
    r.update(kw)
    return r


class RequestTests(unittest.TestCase):
    def test_clean_copy(self):
        r = rv.check_request(req(extra="dropped", count="2"))
        self.assertNotIn("extra", r)
        self.assertEqual(r["count"], 2)
        self.assertEqual(r["marks"][0]["n"], 1)
        self.assertTrue(r["keep_rest"])

    def test_refused(self):
        for bad in (req(count=11), req(strength=0.05), req(marks=[{"text": "x", "box": [0, 0, 2, 1]}]),
                    req(marks=[], note=" "), req(source=None), req(marks=[{"text": "x", "box": [0, 0, 1, 1]}] * 13)):
            with self.assertRaises(ValueError):
                rv.check_request(bad)

    def test_prompts(self):
        r = rv.check_request(req(note="warmer light"))
        p = rv.edit_prompt(r, annotated=True)
        self.assertIn("1. In the top left of the image (mark 1): make the cup a microphone", p)
        self.assertIn("Overall: warmer light", p)
        self.assertIn("Do not draw the marks", p)
        self.assertNotIn("(mark", rv.edit_prompt(r, annotated=False))
        self.assertEqual(rv.where([0.4, 0.4, 0.6, 0.6]), "center")
        self.assertEqual(rv.where([0.7, 0.7, 0.9, 0.9]), "bottom right")


class GraphTests(unittest.TestCase):
    def test_edit_graph_drops_missing_images(self):
        g = fr.edit_graph(fm.graph_template(fm.BUILTIN["qwen21"], "edit_graph"), "p", 5, ["storyboard/a.png"], "x")
        loads = [n for n in g.values() if n["class_type"] == "LoadImage"]
        self.assertEqual([n["inputs"]["image"] for n in loads], ["storyboard/a.png"])
        self.assertFalse(any("{{" in json.dumps(n) for n in g.values()))
        enc = next(n for n in g.values() if n["class_type"] == "TextEncodeQwenImage21")
        self.assertIn("images.image_1", enc["inputs"])
        self.assertNotIn("images.image_2", enc["inputs"])

    def test_inpaint_replaces_empty_latent(self):
        g = fr.inpaint_graph(fm.graph_template(fm.BUILTIN["moss-dmd"]), "p", 5, "storyboard/a.png", "storyboard/m.png",
                             0.8, "x")
        self.assertFalse(any(n["class_type"].startswith("Empty") for n in g.values()))
        ks = next(n for n in g.values() if n["class_type"] == "KSampler")
        self.assertEqual(ks["inputs"]["latent_image"], ["noise_mask", 0])
        self.assertEqual(ks["inputs"]["denoise"], 0.8)
        self.assertEqual(g["noise_mask"]["inputs"]["mask"], ["msk", 1])
        nomask = fr.inpaint_graph(fm.graph_template(fm.BUILTIN["moss"]), "p", 5, "a.png", None, 0.6, "x")
        self.assertNotIn("noise_mask", nomask)

    def test_composite(self):
        g = fr.composite_graph("o.png", "e.png", "m.png", "x")
        self.assertEqual(g["c"]["inputs"]["mask"], ["m", 1])
        self.assertTrue(g["c"]["inputs"]["resize_source"])


class MultipartTests(unittest.TestCase):
    def test_openai_edit_body_parses(self):
        from email.parser import BytesParser
        from email.policy import HTTP
        png = b"\x89PNG\r\n"
        body, ctype = fm._multipart({"model": "m", "prompt": "make it red"},
                                    [("image[]", "ref0.png", png + b"1", "image/png"),
                                     ("image[]", "ref1.png", png + b"2", "image/png"),
                                     ("mask", "mask.png", png + b"m", "image/png")])
        msg = BytesParser(policy=HTTP).parsebytes(f"Content-Type: {ctype}\r\n\r\n".encode() + body)
        parts = {(x.get_param("name", header="content-disposition"), x.get_filename()): x.get_payload(decode=True)
                 for x in msg.iter_parts()}
        self.assertEqual(parts[("image[]", "ref1.png")], png + b"2")
        self.assertEqual(parts[("mask", "mask.png")], png + b"m")
        self.assertEqual(parts[("prompt", None)], b"make it red")


class VersionTests(unittest.TestCase):
    def test_versions_keep_the_first_frame(self):
        s = {"id": "SH01", "start_frame": "a.png"}
        rv.add_versions(s, [{"path": "b.png", "model": "moss"}, {"path": "c.png", "model": "moss"}])
        self.assertEqual([v["path"] for v in s["versions"]], ["a.png", "b.png", "c.png"])
        self.assertEqual(s["start_frame"], "c.png")
        self.assertEqual(s["status"], "frames")
        bd.validate({"shots": [s]})
        s["versions"].append({"path": 3})
        with self.assertRaises(ValueError):
            bd.validate({"shots": [s]})


class JobTests(unittest.TestCase):
    def test_revise_job_writes_request_file(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = bd.Config.from_env(root=Path(d), boards=Path(d) / "storyboards", comfy_url="http://127.0.0.1:1")
            bd.create(cfg, "demo")
            b, _ = bd.load(cfg, "demo")
            frames = cfg.board_dir("demo") / "frames"
            frames.mkdir(exist_ok=True)
            (frames / "SH01.png").write_bytes(b"x")
            b["shots"] = [{"id": "SH01", "duration": 2, "start_frame": "storyboards/demo/frames/SH01.png"}]
            bd.save(cfg, "demo", b)
            jobs = Jobs(cfg)
            jobs.models = FakeChecker(fm.required_files(fm.graph_template(fm.BUILTIN["moss"])).values())
            jobs.q.put = lambda item: None  # do not run it
            job = jobs.submit("revise", "demo", ["SH01"], revision=req(note="a; rm -rf /"))
            self.assertEqual(job["action"], "revise")
            files = list((cfg.board_dir("demo") / "revisions").glob("SH01_*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text())["note"], "a; rm -rf /")
            with self.assertRaisesRegex(ValueError, "not a file in the board"):
                jobs.submit("revise", "demo", ["SH01"], revision=req(source="../../etc/passwd"))
            with self.assertRaisesRegex(ValueError, "not available"):
                jobs.submit("revise", "demo", ["SH01"], revision=req(model="qwen21"))


if __name__ == "__main__":
    unittest.main()
