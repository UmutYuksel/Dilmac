"""OPUS-MT on this computer: the model archive, sentence-by-sentence translation, no caching, the GUI's engine choice."""
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import opus_mt
import translation as T


def fake_archive(folder: Path, nested: bool = True) -> Path:
    path = folder / "model.zip"
    with zipfile.ZipFile(path, "w") as z:
        prefix = "opus-mt-en-tr/" if nested else ""
        for name in opus_mt.REQUIRED + ("shared_vocabulary.json",):
            z.writestr(prefix + name, b"x" * 10)
    return path


class ArchiveTest(unittest.TestCase):
    def test_install_nested_archive(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            archive = fake_archive(d)
            sha = hashlib.sha256(archive.read_bytes()).hexdigest()
            target = d / "models" / "opus-mt-en-tr"
            opus_mt.install_archive(archive, target, sha)
            self.assertTrue(opus_mt.installed(target))
            self.assertFalse((d / "models" / "opus-mt-en-tr.tmp").exists())

    def test_bad_checksum_keeps_old_model(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            target = d / "models" / "opus-mt-en-tr"
            opus_mt.install_archive(fake_archive(d, nested=False), target)
            with self.assertRaises(opus_mt.OpusError):
                opus_mt.install_archive(fake_archive(d), target, "0" * 64)
            self.assertTrue(opus_mt.installed(target))

    def test_download_checks_sha(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as z:
            for name in opus_mt.REQUIRED:
                z.writestr(name, b"y")
        body = data.getvalue()

        class Answer(io.BytesIO):
            headers = {"Content-Length": str(len(body))}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "m"
            with mock.patch("urllib.request.urlopen", return_value=Answer(body)):
                with self.assertRaises(opus_mt.OpusError):
                    opus_mt.download(url="https://x/y.zip", sha256="0" * 64, model_dir=target)
            self.assertFalse(opus_mt.installed(target))
            with mock.patch("urllib.request.urlopen", return_value=Answer(body)):
                seen = []
                opus_mt.download(lambda a, b: seen.append((a, b)), url="https://x/y.zip",
                                 sha256=hashlib.sha256(body).hexdigest(), model_dir=target)
            self.assertTrue(opus_mt.installed(target))
            self.assertEqual(seen[-1], (len(body), len(body)))


class FakeModel:
    def __init__(self):
        self.calls = []

    def translate(self, sentences):
        self.calls.append(list(sentences))
        return [f"<{s}>" for s in sentences]


class EngineTest(unittest.TestCase):
    def test_sentence_by_sentence_and_no_cache(self):
        fake = FakeModel()
        tr = T.Translator({"engine": "opus"}, None, log=lambda *a: None, cache_data=None)
        with mock.patch.object(opus_mt, "model", return_value=fake):
            out, source = tr.translate("Ugh, fuck, fine! Asshole...")
        self.assertEqual(source, "Bilgisayar")
        self.assertEqual(fake.calls, [["Ugh, fuck, fine!", "Asshole..."]])  # one batch, each sentence on its own
        self.assertEqual(out, "<Ugh, fuck, fine!> <Asshole...>")
        self.assertEqual(tr.cache, {})

    def test_failure_falls_back_to_google(self):
        tr = T.Translator({"engine": "opus"}, None, log=lambda *a: None, cache_data=None)
        with mock.patch.object(opus_mt, "model", side_effect=opus_mt.OpusError("yok")), \
                mock.patch.object(T.Translator, "_google", return_value="Polis!"):
            self.assertEqual(tr.translate("Police!"), ("Polis!", "Google"))

    def test_default_engine(self):
        import subtitler as A
        self.assertEqual(A.DEFAULTS["engine"], "opus")


@unittest.skipUnless(opus_mt.available() and opus_mt.installed(), "OPUS-MT modeli indirilmemiş")
class RealModelTest(unittest.TestCase):
    def test_translates(self):
        out = opus_mt.model().translate(["We need to get out of here.", "Hey! Police!"])
        self.assertEqual(len(out), 2)
        self.assertIn("buradan", out[0].lower())


class GuiEngineTest(unittest.TestCase):
    def test_save_engine(self):
        import gui_web
        import subtitler as A

        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text("{}", encoding="utf-8")
            with mock.patch.object(A, "CONFIG_PATH", cfg):
                api = gui_web.Api(type("B", (), {"emit": lambda *a, **k: None, "window": None})(), start_checker=False)
                self.assertTrue(api.save_settings({"engine": "opus"}, restart=False)["ok"])
                self.assertEqual(json.loads(cfg.read_text(encoding="utf-8"))["engine"], "opus")
                self.assertFalse(api.save_settings({"engine": "deepl"}, restart=False)["ok"])
                status = api._engine_status(A.load_config())
                self.assertEqual(status.data_["kind"], "opus")


if __name__ == "__main__":
    unittest.main()
