"""Audio buffer and clip conversion (game audio from chiaki, for recognizing the speaker by voice)."""
import math
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audio as S  # noqa: E402


def stereo_sine(secs: float, rate: int = 48000, hz: float = 440.0, amplitude: int = 8000) -> bytes:
    n = int(secs * rate)
    return b"".join(struct.pack("<hh", int(amplitude * math.sin(2 * math.pi * hz * i / rate)),
                                int(amplitude * math.sin(2 * math.pi * hz * i / rate))) for i in range(n))


class BufferTest(unittest.TestCase):
    def test_segment_by_time(self):
        t = S.AudioBuffer(seconds=5)
        piece_ = b"\x01\x00\x01\x00" * 480  # 10 ms stereo 48 kHz
        for i in range(300):  # 3 s, one chunk every 10 ms
            t.add(piece_, 480, 2, 48000, ts=100.0 + i * 0.01)
        pcm, channels, rate = t.segment(101.0, 102.0)
        self.assertEqual((channels, rate), (2, 48000))
        self.assertAlmostEqual(len(pcm) / (480 * 4), 101, delta=2)  # ~1 s = ~100 chunks

    def test_old_dropped_reset_on_format_change(self):
        t = S.AudioBuffer(seconds=1)
        for i in range(300):
            t.add(b"\x00\x00" * 960, 480, 2, 48000, ts=i * 0.01)
        pcm, _, _ = t.segment(-10, 10)
        self.assertLessEqual(len(pcm) / 1920, 102)
        t.add(b"\x00\x00" * 160, 160, 1, 16000, ts=3.0)
        pcm, channels, rate = t.segment(-10, 10)
        self.assertEqual((len(pcm), channels, rate), (320, 1, 16000))


class ConvertTest(unittest.TestCase):
    def test_mono16k_and_wav(self):
        pcm = stereo_sine(1.0)
        mono = S.mono16k(pcm, 2, 48000)
        self.assertAlmostEqual(len(mono) / 2, 16000, delta=20)
        self.assertGreater(S.loudness(mono), 3000)  # RMS of a sine with amplitude 8000 is ~5600
        self.assertLess(S.loudness(b"\x00\x00" * 1000), 1)
        with tempfile.TemporaryDirectory() as d:
            path_ = Path(d) / "a" / "b.wav"
            S.write_wav(path_, mono)
            self.assertEqual(S.read_wav(path_), mono)

    def test_empty(self):
        self.assertEqual(S.mono16k(b"", 2, 48000), b"")
        self.assertEqual(S.loudness(b""), 0.0)


class FakeExtractor:
    """Instead of a voice vector: the signal's zero-crossing rate (frequency) is turned into a direction. Same frequency
    = same person."""

    class Track:
        def __init__(self):
            self.a = None

        def accept_waveform(self, rate, a):
            self.a = a

        def input_finished(self):
            pass

    def create_stream(self):
        return self.Track()

    def compute(self, st):
        import numpy as np

        a = np.asarray(st.a)
        f = float(np.count_nonzero(np.diff(np.sign(a)))) / 2 / (len(a) / 16000)
        return [math.cos(f / 300), math.sin(f / 300), 0.05]


def mono_sine(secs: float, hz: float, amplitude: int = 8000) -> bytes:
    return S.mono16k(stereo_sine(secs, hz=hz, amplitude=amplitude), 2, 48000)


class RecognizerTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.root = Path(self.d.name)
        self.t = S.SpeakerRecognizer(self.root, "GTA", FakeExtractor())

    def tearDown(self):
        self.d.cleanup()

    def test_no_decision_on_short_or_quiet(self):
        self.assertIsNone(self.t.vector(mono_sine(0.5, 200)))
        self.assertIsNone(self.t.vector(bytes(64000)))  # 2 s of silence
        self.assertIsNotNone(self.t.vector(mono_sine(1.2, 200)))

    def test_learn_and_recognize(self):
        lucia, jason = self.t.vector(mono_sine(1.5, 220)), self.t.vector(mono_sine(1.5, 700))
        for _ in range(S.MIN_PROFILE_SAMPLES):
            self.t.learn("Lucia", lucia)
            self.t.learn("Jason", jason)
        self.assertFalse(self.t.learn("pedestrian", lucia))  # a group label is not used for a profile
        self.assertEqual(self.t.speaker(self.t.vector(mono_sine(1.2, 225)))[0], "Lucia")
        self.assertEqual(self.t.speaker(self.t.vector(mono_sine(1.2, 690)))[0], "Jason")
        self.assertIsNone(self.t.speaker(self.t.vector(mono_sine(1.2, 460)))[0])  # in between the two: no decision

    def test_small_profile_unused_and_saved(self):
        v = self.t.vector(mono_sine(1.5, 220))
        for _ in range(S.MIN_PROFILE_SAMPLES - 1):
            self.t.learn("Lucia", v)
        self.assertIsNone(self.t.speaker(v)[0])  # one clip short of a usable profile
        self.t.learn("Lucia", v)
        self.t.save()
        t2 = S.SpeakerRecognizer(self.root, "GTA", FakeExtractor())
        self.assertEqual(t2.profiles["Lucia"]["n"], S.MIN_PROFILE_SAMPLES)
        self.assertEqual(t2.speaker(v)[0], "Lucia")
        self.assertEqual(S.SpeakerRecognizer(self.root, "Başka oyun", FakeExtractor()).profiles, {})

    def test_small_side_profile_does_not_decide_or_block(self):
        """2026-11-24: Wyman's 15 clips from one noisy scene claimed pedestrians. A profile below MIN_PROFILE_SAMPLES
        neither decides nor counts as the second nearest that blocks a main character's decision."""
        lucia = self.t.vector(mono_sine(1.5, 220))
        for _ in range(S.MIN_PROFILE_SAMPLES):
            self.t.learn("Lucia", lucia)
        near = self.t.vector(mono_sine(1.5, 230))
        for _ in range(S.MIN_PROFILE_SAMPLES - 5):
            self.t.learn("Wyman", near)
        self.assertEqual(self.t.speaker(near)[0], "Lucia")
        self.assertEqual(self.t.speaker(near, min_samples=3)[0], None)  # with Wyman counted the two are too close


    def test_save_adds_to_rebuilt_file(self):
        """2026-11-24: the app saved its in-memory profiles at session end and overwrote a rebuild done meanwhile."""
        v = self.t.vector(mono_sine(1.5, 220))
        app = S.SpeakerRecognizer(self.root, "GTA", FakeExtractor())   # app opened before the rebuild
        tool = S.SpeakerRecognizer(self.root, "GTA", FakeExtractor())  # voice_tool profile
        tool.reset()
        for _ in range(17):
            tool.learn("Jason", v)
        tool.save()
        app.learn("Jason", v)  # learned during play
        app.learn("Lucia", v)
        app.save()
        again = S.SpeakerRecognizer(self.root, "GTA", FakeExtractor())
        self.assertEqual((again.profiles["Jason"]["n"], again.profiles["Lucia"]["n"]), (18, 1))


class RealModelTest(unittest.TestCase):
    """With the installed TitaNet model: the vector has 192 dimensions and unit length (skipped without the model)."""

    def test_vector(self):
        import paths

        t = S.SpeakerRecognizer.open(paths.VOICES, "GTA")
        if t is None:
            self.skipTest("ses modeli ya da sherpa-onnx yok")
        v = t.vector(mono_sine(1.5, 180, amplitude=3000))
        self.assertEqual(len(v), 192)
        self.assertAlmostEqual(float((v * v).sum()), 1.0, places=4)


if __name__ == "__main__":
    unittest.main()
