import http.client
import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path

from storyboard import board as bd
from storyboard import frames as fr
from storyboard.server import make_handler


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = bd.Config.from_env(root=root, boards=root / "storyboards", comfy_url="http://127.0.0.1:1")

    def tearDown(self):
        self.tmp.cleanup()


class BoardTests(Base):
    def test_create_save_history_and_timings(self):
        bd.create(self.cfg, "demo", kind="story", brief="a test")
        b, v1 = bd.load(self.cfg, "demo")
        b["shots"] = [{"id": "SH01", "duration": 2.5}, {"id": "SH02", "duration": 3}]
        v2 = bd.save(self.cfg, "demo", b)
        self.assertNotEqual(v1, v2)
        self.assertEqual(bd.timings(b), [(0.0, 2.5), (2.5, 5.5)])
        self.assertEqual(len(list((self.cfg.board_dir("demo") / "history").glob("*.json"))), 1)

    def test_names_are_validated(self):
        for bad in ("../x", "Upper", "", "a/b", "x" * 70):
            with self.assertRaises(ValueError):
                bd.create(self.cfg, bad)

    def test_rename_rewrites_paths_and_duplicate_keeps_original(self):
        bd.create(self.cfg, "one")
        b, _ = bd.load(self.cfg, "one")
        b["shots"] = [{"id": "SH01", "duration": 1, "start_frame": "storyboards/one/frames/a.png"}]
        bd.save(self.cfg, "one", b)
        bd.rename(self.cfg, "one", "two", copy=True)
        self.assertTrue(self.cfg.board_file("one").exists())
        self.assertEqual(bd.load(self.cfg, "two")[0]["shots"][0]["start_frame"], "storyboards/two/frames/a.png")
        bd.rename(self.cfg, "one", "three")
        self.assertFalse(self.cfg.board_file("one").exists())

    def test_delete_moves_to_trash(self):
        bd.create(self.cfg, "gone")
        dst = bd.delete(self.cfg, "gone")
        self.assertTrue((dst / "storyboard.json").exists())
        self.assertNotIn("gone", bd.names(self.cfg))

    def test_resolve_stays_inside_root(self):
        self.assertIsNone(self.cfg.resolve("../outside.txt"))
        self.assertIsNotNone(self.cfg.resolve("storyboards/x.png"))

    def test_validate_rejects_unsafe_values(self):
        bd.create(self.cfg, "v")
        b, _ = bd.load(self.cfg, "v")
        bad = [
            {"shots": [{"id": "../../evil", "duration": 1}]},
            {"shots": [{"id": "a b", "duration": 1}]},
            {"shots": [{"id": "SH01", "duration": 1}, {"id": "SH01", "duration": 1}]},
            {"shots": [{"id": "SH01", "duration": "1"}]},
            {"shots": [], "width": "<img src=x onerror=alert(1)>"},
            {"shots": [], "kind": "<b>"},
            {"shots": [], "audio": {"file": "a.wav", "bpm": "<x>"}},
            {"shots": [{"id": "SH01", "duration": 1, "start_frame": {"x": 1}}]},
        ]
        for case in bad:
            with self.assertRaises(ValueError, msg=case):
                bd.save(self.cfg, "v", {**b, **case})
        bd.save(self.cfg, "v", {**b, "shots": [{"id": "SH-01_b", "duration": 2.5, "status": "approved"}]})

    def test_production_fields(self):
        bd.create(self.cfg, "p")
        b, _ = bd.load(self.cfg, "p")
        good = {"id": "T6", "duration": 3, "lane": "B", "motion": "action", "workflow": "movement-pikah3",
                "reference_video": "storyboards/p/refs/x.mp4", "landing_pose": False,
                "beats": [{"t": 0.0, "action": "wind-up"}, {"t": 0.8, "action": "strike lands", "impact": True}]}
        bd.save(self.cfg, "p", {**b, "shots": [good],
                                 "characters": [{"id": "A", "desc": "a man", "block": "CHARACTER A ...",
                                                 "voice_id": "abc123", "pack": "zyra_oon"}]})
        bad = [{"lane": "Z"}, {"motion": "fast"}, {"workflow": "../x"}, {"landing_pose": "yes"},
               {"beats": [{"t": -1}]}, {"beats": [{"t": 1, "impact": "yes"}]}, {"beats": "0.8 strike"}]
        for case in bad:
            with self.assertRaises(ValueError, msg=case):
                bd.save(self.cfg, "p", {**b, "shots": [{"id": "S", "duration": 1, **case}]})
        for c in ({"id": "A", "voice_id": "bad id!"}, {"id": "A", "pack": "../x"}, {"id": "A", "block": 3}):
            with self.assertRaises(ValueError, msg=c):
                bd.save(self.cfg, "p", {**b, "characters": [c]})

    def test_motion_default(self):
        self.assertEqual(bd.shot_motion({}), "dialogue")
        self.assertEqual(bd.shot_motion({"lane": "C"}), "ambient")
        self.assertEqual(bd.shot_motion({"lane": "A", "motion": "action"}), "action")

    def test_old_boards_still_load(self):
        bd.create(self.cfg, "old")
        b, _ = bd.load(self.cfg, "old")
        bd.save(self.cfg, "old", {**b, "shots": [{"id": "SH01", "duration": 2, "camera": "Wide"}]})
        self.assertEqual(bd.load(self.cfg, "old", check=True)[0]["shots"][0]["id"], "SH01")

    def test_export_carries_production_fields(self):
        import subprocess
        import sys
        bd.create(self.cfg, "ex")
        d = self.cfg.board_dir("ex")
        (d / "frames").mkdir(exist_ok=True)
        (d / "refs").mkdir(exist_ok=True)
        (d / "frames" / "s.png").write_bytes(b"png")
        (d / "refs" / "move.mp4").write_bytes(b"mp4")
        b, _ = bd.load(self.cfg, "ex")
        b["characters"] = [{"id": "KADE", "desc": "a man, orange jumpsuit", "block": "KADE BLOCK", "voice_id": "v1",
                            "pack": "kade"}]
        b["shots"] = [{"id": "T7", "duration": 3, "lane": "C", "motion": "action", "workflow": "movement-pikah3",
                       "start_frame": "storyboards/ex/frames/s.png", "reference_video": "storyboards/ex/refs/move.mp4",
                       "beats": [{"t": 0.5, "action": "turn", "impact": False}], "characters": ["KADE"],
                       "speaker": "KADE", "dialogue": "Go.", "status": "approved"},
                      {"id": "T8", "duration": 2, "lane": "C", "start_frame": "storyboards/ex/frames/s.png",
                       "status": "approved"}]
        bd.save(self.cfg, "ex", b)
        root = Path(self.tmp.name)
        r = subprocess.run([sys.executable, "-m", "storyboard", "--root", str(root), "--boards", str(root / "storyboards"),
                            "export", "ex", "--approved"], capture_output=True, text=True,
                           cwd=Path(__file__).resolve().parent.parent)
        self.assertEqual(r.returncode, 0, r.stderr)
        t7 = json.loads((d / "export" / "T7" / "shot.json").read_text(encoding="utf-8"))
        self.assertEqual((t7["lane"], t7["motion"], t7["workflow"]), ("C", "action", "movement-pikah3"))
        self.assertEqual(t7["reference_video"], "reference.mp4")
        self.assertTrue((d / "export" / "T7" / "reference.mp4").exists())
        self.assertEqual(t7["character_blocks"], {"KADE": "KADE BLOCK"})
        self.assertEqual((t7["voice_id"], t7["packs"]), ("v1", {"KADE": "kade"}))
        self.assertEqual(t7["beats"][0]["action"], "turn")
        self.assertFalse(t7["landing_pose"])
        t8 = json.loads((d / "export" / "T8" / "shot.json").read_text(encoding="utf-8"))
        self.assertEqual(t8["motion"], "ambient")

    def test_requests(self):
        bd.create(self.cfg, "r", request="draft it")
        b, _ = bd.load(self.cfg, "r")
        b["shots"] = [{"id": "SH01", "duration": 1, "request": "wider"}]
        bd.save(self.cfg, "r", b)
        self.assertEqual(bd.requests(self.cfg), [("r", None, "draft it"), ("r", "SH01", "wider")])


class PageTests(unittest.TestCase):
    def test_page_script_parses(self):
        """Syntax-check the page's script with Node when it is installed (a stray newline in a string once broke it)."""
        import shutil
        import subprocess
        if not shutil.which("node"):
            self.skipTest("node not installed")
        page = (Path(__file__).resolve().parent.parent / "storyboard" / "web" / "board.html").read_text(encoding="utf-8")
        script = page[page.index("<script>") + 8:page.rindex("</script>")]
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(script)
        try:
            r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        finally:
            Path(f.name).unlink()


class FrameTests(unittest.TestCase):
    def test_sketch_prompt_keeps_names_and_lens_out(self):
        board = {"characters": [{"id": "KADE", "desc": "a man in his 30s, orange jumpsuit"}]}
        shot = {"id": "SH01", "action": "Kade turns Kade's key.", "characters": ["KADE"],
                "cam": {"size": "Close-up", "angle": "Low angle", "lens": "85mm", "move": "Push-in"}}
        p = fr.frame_prompt(board, shot, "sketch")
        self.assertNotIn("Kade", p)
        self.assertNotIn("85mm", p)
        self.assertNotIn("Push-in", p)
        self.assertIn("a man in his 30s's key", p)
        self.assertIn("Close-up low angle view", p)

    def test_template_placeholders(self):
        t = {"1": {"class_type": "X", "inputs": {"text": "{{prompt}}", "seed": "{{seed}}", "w": "{{width}}"}},
             "2": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}}}
        g = fr.template_graph(t, "hello", 7, 640, 360, "pre")
        self.assertEqual(g["1"]["inputs"], {"text": "hello", "seed": 7, "w": 640})
        self.assertEqual(g["2"]["inputs"]["filename_prefix"], "pre")

    def test_frame_size_keeps_aspect(self):
        w, h = fr.frame_size({"width": 720, "height": 1280}, "sketch")
        self.assertLess(w, h)
        self.assertEqual((w % 16, h % 16), (0, 0))


class ServerTests(Base):
    def setUp(self):
        super().setUp()
        bd.create(self.cfg, "demo")
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.cfg))
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        c.request(method, path, data, headers or {})
        r = c.getresponse()
        out = r.read()
        c.close()
        return r.status, out

    def test_page_and_board(self):
        self.assertEqual(self.req("GET", "/")[0], 200)
        st, out = self.req("GET", "/api/board?b=demo")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(out)["board"]["shots"], [])

    def test_page_has_csp_with_nonce(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/")
        r = c.getresponse()
        page = r.read().decode()
        csp = r.getheader("Content-Security-Policy")
        c.close()
        nonce = csp.split("'nonce-")[1].split("'")[0]
        self.assertIn(f'<script nonce="{nonce}">', page)
        self.assertNotIn("unsafe-eval", csp)

    def test_put_rejects_bad_shot_id(self):
        h = {"X-Storyboard": "1", "Content-Type": "application/json"}
        j = json.loads(self.req("GET", "/api/board?b=demo")[1])
        b = {**j["board"], "shots": [{"id": "../../x", "duration": 1}]}
        self.assertEqual(self.req("PUT", "/api/board?b=demo", {"version": j["version"], "board": b}, h)[0], 400)

    def test_writes_need_header(self):
        st, _ = self.req("POST", "/api/boards", {"action": "delete", "name": "demo"})
        self.assertEqual(st, 403)
        self.assertTrue(self.cfg.board_file("demo").exists())

    def test_foreign_host_refused(self):
        self.assertEqual(self.req("GET", "/api/boards", headers={"Host": "evil.example"})[0], 403)

    def test_save_conflict(self):
        h = {"X-Storyboard": "1", "Content-Type": "application/json"}
        _, out = self.req("GET", "/api/board?b=demo")
        j = json.loads(out)
        b = j["board"]
        b["shots"] = [{"id": "SH01", "duration": 2}]
        self.assertEqual(self.req("PUT", "/api/board?b=demo", {"version": j["version"], "board": b}, h)[0], 200)
        self.assertEqual(self.req("PUT", "/api/board?b=demo", {"version": j["version"], "board": b}, h)[0], 409)

    def test_upload_confined(self):
        h = {"X-Storyboard": "1"}
        st, _ = self.req("POST", "/api/upload?b=demo&shot=SH01&slot=../../x&name=a.png", b"x", h)
        self.assertEqual(st, 400)
        st, _ = self.req("POST", "/api/upload?b=demo&shot=SH01&slot=start&name=a.exe", b"x", h)
        self.assertEqual(st, 400)
        st, out = self.req("POST", "/api/upload?b=demo&shot=../SH01&slot=start&name=a.png", b"png", h)
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(out)["path"].startswith("storyboards/demo/frames/"))

    def test_file_outside_root_refused(self):
        self.assertEqual(self.req("GET", "/file?p=../../etc/passwd")[0], 404)


if __name__ == "__main__":
    unittest.main()
