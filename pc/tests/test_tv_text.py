"""Text size on the TV: a fixed size, independent of the row height OCR measures."""
import json
import socket
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import subtitler as A  # noqa: E402
import checks as B  # noqa: E402


class FontSizeTest(unittest.TestCase):
    def test_default_matches_game_subtitle(self):
        self.assertEqual(A.font_px({}), A.BASE_FONT_PX)
        self.assertEqual(A.font_px({"font_px": None, "font_scale": 1.0}), A.BASE_FONT_PX)

    def test_old_scale_setting_becomes_px(self):
        self.assertEqual(A.font_px({"font_px": None, "font_scale": 1.5}), 48)

    def test_px_overrides_scale_and_clamps(self):
        self.assertEqual(A.font_px({"font_px": 40, "font_scale": 2.0}), 40)
        self.assertEqual(A.font_px({"font_px": 500}), 96)
        self.assertEqual(A.font_px({"font_px": 3}), 12)

    def test_line_height_screen_ratio(self):
        self.assertAlmostEqual(A.line_height({"font_px": 54}), 0.05)
        self.assertAlmostEqual(A.line_height({"font_px": 54}, "objective"), round(54 * A.OBJECTIVE_RATIO / 1080, 4))


class SampleTextTest(unittest.TestCase):
    def test_send_tv_text_fixed_px(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        received = []

        def accepted():
            c, _ = server.accept()
            with c:
                received.append(c.makefile(encoding="utf-8").readline())

        t = threading.Thread(target=accepted, daemon=True)
        t.start()
        with mock.patch.object(B, "TV_PORT", server.getsockname()[1]):
            self.assertTrue(B.send_tv_text("127.0.0.1", "deneme", {"font_px": 54, "font_scale": 3.0}))
        t.join(2)
        server.close()
        m = json.loads(received[0])
        self.assertAlmostEqual(m["lineh"], 0.05)
        self.assertEqual(m["scale"], 1.0)


class AdbPathTest(unittest.TestCase):
    """2026-11-24: the exe could not find adb (it was only in the Android SDK of another environment); the adb shipped
    inside the exe must come first."""

    def test_bundled_adb_first(self):
        import tempfile
        from unittest import mock

        import checks as B

        with tempfile.TemporaryDirectory() as d:
            adb = Path(d) / "adb" / "adb.exe"
            adb.parent.mkdir()
            adb.write_bytes(b"")
            with mock.patch.object(B.paths, "BUNDLE", Path(d)):
                self.assertEqual(B.adb_path(), adb)


if __name__ == "__main__":
    unittest.main()
