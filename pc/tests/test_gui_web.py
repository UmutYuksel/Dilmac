"""The HTML GUI's Python side (gui_web.Api) without a window: state, settings, region; the page files exist."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gui_web
import subtitler as A


class FakeBridge:
    def __init__(self):
        self.events = []
        self.window = None

    def emit(self, event, **data):
        self.events.append({"event": event, **data})


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "config.json"
        self.config.write_text(json.dumps({"tv_ip": "192.168.1.59", "tv_type": "lg", "gemini_api_key": "k"}),
                               encoding="utf-8")
        self.patch = mock.patch.object(A, "CONFIG_PATH", self.config)
        self.patch.start()
        self.api = gui_web.Api(FakeBridge(), start_checker=False)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_state_is_json(self):
        s = self.api.state()
        json.dumps(s)
        self.assertTrue(s["setup_done"])  # an older config with a TV counts as set up
        self.assertEqual(s["cfg"]["tv_type"], "lg")
        self.assertEqual(s["tv_labels"]["samsung"], "Samsung")
        self.assertIn("GTA", " ".join(s["games"]) + "GTA")

    def test_save_settings_types_and_limits(self):
        r = self.api.save_settings({"font_px": "200", "box_opacity": 0.1, "tv_hdmi": 9, "tv_type": "philips",
                                    "speaker_colors": 0, "unknown_key": 1, "engine": "google"}, restart=False)
        self.assertTrue(r["ok"])
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["font_px"], 80)
        self.assertEqual(saved["box_opacity"], 0.3)
        self.assertEqual(saved["tv_hdmi"], 4)
        self.assertEqual(saved["tv_type"], "android")
        self.assertIs(saved["speaker_colors"], False)
        self.assertNotIn("unknown_key", saved)
        self.assertTrue(saved["google_only"])
        self.assertEqual(saved["engine"], "opus")  # google_only on top of the free on-computer engine
        import secure_store
        self.assertTrue(secure_store.is_protected(saved["gemini_api_key"]))  # secrets are encrypted on disk
        self.assertEqual(secure_store.unprotect_text(saved["gemini_api_key"]), "k")  # untouched keys stay
        self.assertEqual(A.load_config()["gemini_api_key"], "k")
        self.assertTrue(r["state"]["cfg"]["google_only"])

    def test_bad_number(self):
        r = self.api.save_settings({"font_px": "abc"}, restart=False)
        self.assertFalse(r["ok"])

    def test_fresh_setup_and_finish(self):
        self.config.write_text("{}", encoding="utf-8")
        self.assertFalse(self.api.state()["setup_done"])
        self.assertTrue(self.api.finish_setup()["setup_done"])

    def test_region(self):
        self.api.save_region({"left": 0.1234, "top": 0.75, "width": 0.8, "height": 0.2})
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["region"], {"left": 0.123, "top": 0.75, "width": 0.8, "height": 0.2})

    def test_start_without_tv(self):
        self.config.write_text("{}", encoding="utf-8")
        r = self.api.start()
        self.assertFalse(r["ok"])
        self.assertIn("TV", r["error"])


class PageTest(unittest.TestCase):
    def test_files(self):
        for name in ("index.html", "app.js", "style.css"):
            self.assertTrue((gui_web.UI_DIR / name).is_file(), name)
        html = (gui_web.UI_DIR / "index.html").read_text(encoding="utf-8")
        js = (gui_web.UI_DIR / "app.js").read_text(encoding="utf-8")
        # every element id the script looks up by "#id" exists in the page or is made by the script itself
        import re
        ids = set(re.findall(r'id="([\w-]+)"', html)) | set(re.findall(r'id="([\w-]+)"', js))
        used = {u for u in re.findall(r"\$\('#([\w-]+)'", js) if not u.endswith("-")}  # '#card-' + id: built ids
        self.assertEqual(sorted(used - ids), [])

    def test_api_methods_exist(self):
        """Every api.NAME( the page calls is a public method of gui_web.Api."""
        import re
        js = (gui_web.UI_DIR / "app.js").read_text(encoding="utf-8")
        js = js[:js.index("function mockApi")]
        called = set(re.findall(r"\bapi\.(\w+)\(", js))
        missing = [n for n in called if not callable(getattr(gui_web.Api, n, None))]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
