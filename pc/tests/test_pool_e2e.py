"""The whole pool chain against the real Worker (server/ in wrangler's local mode): the app queues lines, the nightly
job translates them (OPUS-MT here, Gemma on the Legion) and publishes the pool pack, the app downloads and uses it.
Skipped without Node.js + server/node_modules or without the OPUS-MT model."""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import opus_mt
import pack_server as S
import paths

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
WRANGLER = SERVER / "node_modules" / "wrangler" / "bin" / "wrangler.js"


def find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "ps5altyazi-indirme" / "node"
    for exe in sorted(base.glob("node-*/node.exe"), reverse=True):
        return str(exe)
    return None


NODE = find_node()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@unittest.skipUnless(NODE and WRANGLER.is_file(), "Node.js ya da server/node_modules yok")
@unittest.skipUnless(opus_mt.available() and opus_mt.installed(), "OPUS-MT modeli yok")
class PoolEndToEndTest(unittest.TestCase):
    SECRET, ADMIN = "e2e-secret", "e2e-admin-token-0123456789-abcdefgh"  # the Worker wants 32+ chars

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dilmac-e2e-")
        cls.vars = SERVER / ".dev.vars"
        cls.old_vars = cls.vars.read_bytes() if cls.vars.exists() else None
        cls.vars.write_text(f"APP_SECRET={cls.SECRET}\nADMIN_TOKEN={cls.ADMIN}\n", encoding="utf-8")
        persist = str(Path(cls.tmp) / "wrangler")
        r = subprocess.run([NODE, str(WRANGLER), "d1", "execute", "dilmac-pool", "--local", "--persist-to", persist,
                            "--file", "schema.sql"], cwd=SERVER, capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, r.stderr or r.stdout
        cls.port = free_port()
        cls.dev = subprocess.Popen([NODE, str(WRANGLER), "dev", "--local", "--port", str(cls.port), "--ip", "127.0.0.1",
                                    "--persist-to", persist, "--show-interactive-dev-session=false"],
                                   cwd=SERVER, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.base = f"http://127.0.0.1:{cls.port}"
        for _ in range(120):
            try:
                with urllib.request.urlopen(cls.base + "/v1/health", timeout=1) as resp:
                    if resp.status == 200:
                        break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("wrangler dev açılmadı")

    @classmethod
    def tearDownClass(cls):
        cls.dev.terminate()
        try:
            cls.dev.wait(10)
        except subprocess.TimeoutExpired:
            cls.dev.kill()
        if cls.old_vars is None:
            cls.vars.unlink(missing_ok=True)
        else:
            cls.vars.write_bytes(cls.old_vars)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_lines_to_pack(self):
        work = Path(self.tmp) / "data"
        work.mkdir()
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()  # a publisher key only this test trusts
        _, pub = S.public_key_text(key)
        # Our own PC: lines go with the admin token (trusted), so one sender is enough for the nightly job.
        cfg = {"pack_server": self.base, "pack_secret": self.SECRET, "pool_admin_token": self.ADMIN,
               "engine": "opus", "share_lines": True, "pack_trusted_keys": [pub]}
        curated = json.loads((paths.BUNDLE / "packs" / "gta6.json").read_text(encoding="utf-8"))
        known_line = next(iter(curated["lines"]))
        new_line = "Sixty-seven unicorns are waiting outside the laundromat."
        with mock.patch.object(paths, "DATA", work), mock.patch.object(paths, "SERVER_PACKS", work / "packs-server"), \
                mock.patch.object(S, "signing_key", return_value=key):
            pool = S.LinePool(cfg, log=lambda *a: None, queue_file=work / "q.json", start=False)
            pool.add("Grand Theft Auto VI", new_line, "speech", "Bilgisayar")
            pool.add("Grand Theft Auto VI", known_line, "speech", "önbellek")
            pool.add("Grand Theft Auto VI", "Xq7 4kz# ^^v 9zq", "speech", "Google")
            pool.add("Grand Theft Auto VI", "Skipped because it came from the pack.", "speech", "pack")
            self.assertEqual(pool.flush(), 3)  # the pack-sourced line is not sent
            waiting = S.admin_lines(cfg)
            self.assertEqual(len(waiting), 3)

            import nightly

            logs = []
            summary = nightly.run(cfg, logs.append)
            gta = summary["Grand Theft Auto VI"]
            self.assertEqual((gta["translated"], gta["known"], gta["junk"]), (1, 1, 1), logs)
            self.assertEqual(S.admin_lines(cfg), [])  # nothing waits any more

            # Another PC: downloads the pool pack and finds the new line.
            other = work / "other-pc"
            result = S.sync_packs(cfg, lambda *a: None, folder=other, force=True)
            self.assertTrue(any("havuz" in u for u in result["updated"]), result)
            pool_file = other / "pool-grand-theft-auto-vi.json"
            data = json.loads(pool_file.read_text(encoding="utf-8"))
            self.assertIn(new_line, data["lines"])
            self.assertTrue(data["lines"][new_line])
            with mock.patch.object(paths, "SERVER_PACKS", other):
                from pack import Pack

                pack = Pack.load("Grand Theft Auto VI")
                self.assertEqual(pack.lookup(new_line), data["lines"][new_line])
            self.assertEqual(S.sync_packs(cfg, lambda *a: None, folder=other)["skipped"], "az önce bakıldı")


if __name__ == "__main__":
    unittest.main()
