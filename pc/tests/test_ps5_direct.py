"""Direct PS5 connection: parsing the PSN address, the pairing file, the chiaki bridge (without a PS5), video decoding
and the main loop taking frames from the PS5 stream (with a fake stream)."""
import asyncio
import base64
import ctypes
import json
import sys
import tempfile
import threading
import time
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import av  # noqa: F401
    HAS_AV = True
except Exception:
    HAS_AV = False

import ps5_direct as D  # noqa: E402

HAS_BRIDGE = D.bridge_dir() is not None

REDIRECT = "https://remoteplay.dl.playstation.net/remoteplay/redirect"


class LoginTest(unittest.TestCase):
    def test_login_url(self):
        a = D.login_url()
        self.assertTrue(a.startswith("https://auth.api.sonyentertainmentnetwork.com/2.0/oauth/authorize?"))
        self.assertIn("client_id=ba495a24-818c-472b-b12d-ff231c1b5745", a)
        self.assertIn("redirect_uri=https%3A%2F%2Fremoteplay.dl.playstation.net", a)

    def test_code_from_pasted_url(self):
        self.assertEqual(D.code_from_redirect(f'  "{REDIRECT}?code=AbC123&cid=x"  '), "AbC123")
        self.assertEqual(D.code_from_redirect(f"Adres: {REDIRECT}?cid=1&code=Zz9 sonra"), "Zz9")

    def test_bad_address(self):
        with self.assertRaises(D.PS5Error):
            D.code_from_redirect("https://example.com/?code=1")
        with self.assertRaisesRegex(D.PS5Error, "access_denied"):
            D.code_from_redirect(f"{REDIRECT}?error=access_denied&error_description=User+denied")

    def test_rp_id(self):
        self.assertEqual(base64.b64decode(D.rp_id(0x0102030405060708)), bytes([8, 7, 6, 5, 4, 3, 2, 1]))

    def test_account_info_sony_reply(self):
        class Reply:
            def __init__(self, code, data_):
                self.status_code, self._data = code, data_

            def json(self):
                return self._data

        session = mock.Mock()
        session.post.return_value = Reply(200, {"access_token": "tok"})
        session.get.return_value = Reply(200, {"user_id": "1234567890123456789", "online_id": "ikinci_hesap"})
        self.assertEqual(D.account_info("code", session), ("ikinci_hesap", 1234567890123456789))
        self.assertEqual(session.post.call_args.kwargs["data"]["code"], "code")
        session.post.return_value = Reply(400, {"error": "invalid_grant"})
        with self.assertRaisesRegex(D.PS5Error, "tek kullanımlık"):
            D.account_info("code", session)


class PairingFileTest(unittest.TestCase):
    def test_add_account_and_is_paired(self):
        with tempfile.TemporaryDirectory() as d:
            path_ = Path(d) / "ps5-pairing.json"
            with mock.patch.object(D, "account_info", return_value=("ikinci_hesap", 42)):
                name = D.add_account(f"{REDIRECT}?code=abc", path_)
            self.assertEqual(name, "ikinci_hesap")
            self.assertEqual(D.accounts(path_), ["ikinci_hesap"])
            self.assertFalse(D.is_paired(path_))
            self.assertIsNone(D.paired_name(path_))
            # The file is encrypted for this Windows user: no account id in plain text on disk.
            self.assertNotIn(b"ikinci_hesap", path_.read_bytes())
            # As if already paired (an older plain file is still read): logging in again must not delete the pairing
            data = D._read(path_)
            data["ps5"] = {"aabbccddeeff": {"name": "PS5-955", "host": "192.0.2.1", "regist_key": "00" * 16,
                                            "rp_key": "11" * 16, "date": "2026-11-23 20:00:00"}}
            path_.write_text(json.dumps(data), encoding="utf-8")
            self.assertTrue(D.is_paired(path_))
            self.assertNotIn(b"regist_key", path_.read_bytes())  # encrypted on that first read
            with mock.patch.object(D, "account_info", return_value=("ikinci_hesap", 42)):
                D.add_account(f"{REDIRECT}?code=yeni", path_)
            self.assertTrue(D.is_paired(path_))
            self.assertEqual(D.paired_name(path_), "PS5-955 · ikinci_hesap")

    def test_pin_format_and_account(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(D.PS5Error, "8 haneli"):
                D.pair("192.0.2.1", "1234", path_=Path(d) / "yok.json")
            with self.assertRaisesRegex(D.PS5Error, "PSN hesabıyla giriş"):
                D.pair("192.0.2.1", "1234 5678", path_=Path(d) / "yok.json")


@unittest.skipUnless(HAS_BRIDGE, "chiaki_bridge.dll derlenmemiş (pc/chiaki_bridge/build.sh)")
class BridgeTest(unittest.TestCase):
    """The chiaki bridge without a PS5: it loads, gives error texts, pairing/session with a missing PS5 fails cleanly."""

    def setUp(self):
        # Another test may have run run() and attached chiaki to the app log: these tests' expected errors must not
        # end up in the user's pc\log.txt.
        D.attach_log(None)

    def test_loads(self):
        dll = D.bridge()
        self.assertEqual(dll.kp_version(), D.BRIDGE_VERSION)
        self.assertEqual(D.error_text(0), "Success")

    def test_missing_ps5_pairing_timeout(self):
        dll = D.bridge()
        rk, rp, mac, name = (ctypes.create_string_buffer(16), ctypes.create_string_buffer(16),
                           ctypes.create_string_buffer(6), ctypes.create_string_buffer(64))
        t0 = time.monotonic()
        outcome = dll.kp_register(b"127.0.0.1", (42).to_bytes(8, "little"), 12345678, 1, 1500, rk, rp, mac, name, 64)
        self.assertIn(outcome, (1, 2))  # rejected or timed out; returns without crashing
        self.assertLess(time.monotonic() - t0, 15)

    def test_missing_ps5_session_ends(self):
        dll = D.bridge()
        events = []
        finished = threading.Event()

        def event(kind, code, txt, user):
            events.append(kind)
            if kind == 2:
                finished.set()

        event_cb = D._EVENT_CB(event)
        frame_cb = D._FRAME_CB(lambda *a: 1)
        audio_cb = D._AUDIO_CB(lambda *a: None)  # the session must open and close cleanly with audio requested too
        err = ctypes.c_int(0)
        ptr = dll.kp_session_open(b"127.0.0.1", bytes(16), bytes(16), 1, 4, 30, 0, frame_cb, event_cb, audio_cb, None,
                               ctypes.byref(err))
        if ptr:
            finished.wait(20)
            dll.kp_session_close(ptr)
            self.assertIn(2, events)  # could not connect, the "ended" event came
            self.assertNotIn(1, events)
        else:
            self.assertNotEqual(err.value, 0)

    def test_wakeup_invalid_key(self):
        self.assertNotEqual(D.bridge().kp_wakeup(b"127.0.0.1", b"zz" + bytes(14), 1), 0)


def _h264_packets(n=8):
    import av
    from PIL import Image, ImageDraw

    enc = av.codec.Codec("libx264", "w").create()
    enc.width, enc.height, enc.pix_fmt, enc.time_base = 640, 360, "yuv420p", Fraction(1, 30)
    enc.options = {"tune": "zerolatency", "preset": "ultrafast"}
    enc.open()
    out = []
    for i in range(n):
        im = Image.new("RGB", (640, 360), (20, 20, 30))
        ImageDraw.Draw(im).rectangle([200, 300, 440, 330], fill=(250, 250, 250))
        fr = av.VideoFrame.from_image(im).reformat(format="yuv420p")
        fr.pts = i
        out += [bytes(p) for p in enc.encode(fr)]
    return out + [bytes(p) for p in enc.encode(None)]


@unittest.skipUnless(HAS_AV, "PyAV kurulu değil")
class DecoderTest(unittest.TestCase):
    def test_h264_decodes(self):
        try:
            packets = _h264_packets()
        except Exception as e:  # if there is no encoder
            self.skipTest(f"H.264 kodlayıcı yok: {e}")
        track = D.PS5Stream("192.0.2.1")
        track._decoder = D.Decoder("h264")
        # like chiaki's callback: ctypes pointer + size
        for p in packets:
            buf = (ctypes.c_uint8 * len(p)).from_buffer_copy(p)
            self.assertEqual(track._on_frame(buf, len(p), 0, None), 1)
        self.assertEqual(track._on_frame((ctypes.c_uint8 * 4)(0, 0, 0, 7), 4, 0, None) in (0, 1), True)
        self.assertEqual(track._decoder.frame_count, 8)
        im = track.latest_frame()
        self.assertEqual(im.size, (640, 360))
        self.assertEqual(im.getpixel((320, 315)), (250, 250, 250))
        self.assertIsNone(track.latest_frame(max_age=-1))  # an old frame is not returned


@unittest.skipUnless(HAS_AV, "PyAV kurulu değil")
class StreamTest(unittest.TestCase):
    """PS5Stream with a fake chiaki bridge: wake-up, connecting, frames, stopping, retrying after a drop."""

    def setUp(self):
        try:
            self.packets = _h264_packets()
        except Exception as e:
            self.skipTest(f"H.264 kodlayıcı yok: {e}")

    def fake_bridge(self, standby=False):
        test = self

        class Fake:
            woken = 0
            closed = 0
            opened = 0

            def kp_wakeup(self, host, text_key, ps5):
                self.woken += 1
                status["bekleme"] = False
                return 0

            def kp_session_open(self, host, rk, rp, ps5, res, fps, codec_id, frame, event, audio, user, err):
                self.opened += 1
                self._stop_event = threading.Event()

                def pump():
                    event(1, 0, b"", None)
                    if audio:  # 10 ms of stereo 48 kHz audio
                        audio((ctypes.c_int16 * 960)(*([1000, -1000] * 480)), 480, 2, 48000, None)
                    for p in test.packets:
                        if self._stop_event.is_set():
                            return
                        frame((ctypes.c_uint8 * len(p)).from_buffer_copy(p), len(p), 0, None)
                        time.sleep(0.01)
                threading.Thread(target=pump, daemon=True).start()
                return 1234

            def kp_session_close(self, ptr):
                self.closed += 1
                self._stop_event.set()

            def kp_request_keyframe(self, ptr):
                return 0

            def kp_error_text(self, code):
                return b"error"

        status = {"bekleme": standby}

        def find_ps5(ip="", duration=1.2):
            import checks as B

            return B.Status("warn", "Bekleme modunda · PS5") if status["bekleme"] else B.Status("ok", "Açık · PS5")

        return Fake(), find_ps5

    def test_wakes_connects_frames_stops(self):
        import checks as B

        fake, find_ps5 = self.fake_bridge(standby=True)
        p = {"name": "PS5", "host": "192.0.2.1", "regist_key": "00" * 16, "rp_key": "11" * 16}
        logged = []
        with mock.patch.object(D, "bridge", return_value=fake), mock.patch.object(D, "paired_ps5", return_value=p), \
                mock.patch.object(B, "find_ps5", find_ps5), mock.patch.object(D.time, "sleep", lambda s: None):
            track = D.PS5Stream("192.0.2.1", log=logged.append)
            track.start()
            end_time = time.monotonic() + 10
            while track.latest_frame() is None and time.monotonic() < end_time:
                threading.Event().wait(0.05)
            im = track.latest_frame()
            self.assertIsNotNone(im)
            self.assertEqual(track.status, "connected")
            track.stop()
        self.assertEqual(fake.woken, 1)
        self.assertEqual(fake.closed, 1)
        self.assertEqual(track.status, "stopped")
        self.assertTrue(any("doğrudan bağlandı" in g for g in logged))

    def test_audio_reaches_buffer(self):
        import checks as B
        import audio as S

        fake, find_ps5 = self.fake_bridge()
        p = {"name": "PS5", "host": "192.0.2.1", "regist_key": "00" * 16, "rp_key": "11" * 16}
        buffer = S.AudioBuffer()
        with mock.patch.object(D, "bridge", return_value=fake), mock.patch.object(D, "paired_ps5", return_value=p),                 mock.patch.object(B, "find_ps5", find_ps5):
            track = D.PS5Stream("192.0.2.1", log=lambda s: None, audio=buffer)
            track.start()
            end_time = time.monotonic() + 10
            while buffer.total_samples == 0 and time.monotonic() < end_time:
                threading.Event().wait(0.05)
            track.stop()
        self.assertEqual((buffer.channels, buffer.rate, buffer.total_samples), (2, 48000, 480))
        pcm, channels, rate = buffer.segment(time.monotonic() - 30, time.monotonic())
        self.assertEqual(len(pcm), 480 * 2 * 2)

    def test_events(self):
        track = D.PS5Stream("192.0.2.1", log=lambda s: None)
        track._on_event(3, 0, b"", None)
        self.assertTrue(track._ended.is_set())
        self.assertIn("giriş kodu", track._reason)
        track._ended.clear()
        track._on_event(2, 5, b"Remote Play is in use", None)
        self.assertEqual(track._reason, "Remote Play is in use")


class MainLoopTest(unittest.TestCase):
    """run() in direct mode does not look for a window, it reads the PS5 stream's frame."""

    def test_config_error_if_not_paired(self):
        import subtitler as A

        cfg = A.load_config()
        cfg.update(connection="direct", ps5_ip="192.0.2.1", tv_ip="192.0.2.2", engine="google")
        with mock.patch.object(D, "is_paired", return_value=False), mock.patch.object(A, "_write_log"), \
                mock.patch.object(A, "log_hook"), mock.patch.object(A, "_log_hook", lambda s: None), \
                mock.patch.object(A, "Translator"), mock.patch.object(A, "make_ocr_engine"):
            with self.assertRaisesRegex(A.ConfigError, "eşlenmedi"):
                asyncio.run(A.run(cfg))


if __name__ == "__main__":
    unittest.main()
