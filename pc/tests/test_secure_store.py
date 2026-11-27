"""Secrets at rest (DPAPI): values, files, migration of older plain data, unreadable blobs."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import secure_store as S


class SecureStoreTest(unittest.TestCase):
    def test_value_round_trip(self):
        v = S.protect_text("anahtar-ğüş")
        self.assertTrue(v.startswith("dpapi:"))
        self.assertNotIn("anahtar", v)
        self.assertEqual(S.unprotect_text(v), "anahtar-ğüş")
        self.assertEqual(S.protect_text(v), v)  # not encrypted twice
        self.assertEqual(S.unprotect_text("düz"), "düz")  # older plain value
        self.assertEqual(S.unprotect_text(""), "")
        self.assertEqual(S.unprotect_text(None), "")

    def test_foreign_blob_counts_as_missing(self):
        self.assertEqual(S.unprotect_text("dpapi:AAAA"), "")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x"
            p.write_bytes(S.FILE_MAGIC + b"AAAA\n")
            with self.assertRaises(S.SecretError):
                S.read_bytes(p)

    def test_file_migration(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "lg-tv.json"
            p.write_text(json.dumps({"key": "-----BEGIN RSA PRIVATE KEY-----"}), encoding="utf-8")
            self.assertEqual(S.read_json(p)["key"], "-----BEGIN RSA PRIVATE KEY-----")
            self.assertTrue(S.is_protected_file(p))
            self.assertNotIn(b"PRIVATE KEY", p.read_bytes())
            self.assertEqual(S.read_json(p)["key"], "-----BEGIN RSA PRIVATE KEY-----")

    def test_config_secrets(self):
        import subtitler as A

        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text(json.dumps({"gemini_api_key": "plain-key", "pool_admin_token": "tok", "game": "x"}),
                           encoding="utf-8")
            with mock.patch.object(A, "CONFIG_PATH", cfg):
                loaded = A.load_config()
                self.assertEqual((loaded["gemini_api_key"], loaded["pool_admin_token"]), ("plain-key", "tok"))
                raw = cfg.read_text(encoding="utf-8")
                self.assertNotIn("plain-key", raw)  # an older plain config is encrypted on load
                self.assertNotIn('"tok"', raw)
                self.assertIn('"game": "x"', raw)


if __name__ == "__main__":
    unittest.main()
