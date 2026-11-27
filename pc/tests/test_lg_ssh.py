"""lg_tv's SSH steps against a fake LG developer mode SSH server (paramiko's server side): copy the package without
SFTP, install it through the dev install service, renew the session, open the app with the PC's address."""
import io
import json
import re
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import paramiko

import lg_tv
import paths


class FakeTv(paramiko.ServerInterface):
    """Answers the commands lg_tv sends, like a webOS TV in developer mode (no SFTP subsystem)."""

    def __init__(self, state):
        self.state = state

    def get_allowed_auths(self, username):
        return "publickey"

    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_SUCCESSFUL if username == "prisoner" else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        threading.Thread(target=self.run, args=(channel, command.decode("utf-8")), daemon=True).start()
        return True

    def run(self, chan, cmd):
        # paramiko sends the exec request's "success" only after check_channel_exec_request returns; a real server
        # answers before the command runs, so wait a moment or a fast command closes the channel first.
        time.sleep(0.05)
        st = self.state
        st["commands"].append(cmd)
        code = 0
        if cmd.startswith("cat > "):
            data = bytearray()
            while True:
                chunk = chan.recv(65536)
                if not chunk:
                    break
                data += chunk
            st["files"][cmd[6:]] = bytes(data)
        elif cmd.startswith("wc -c < "):
            chan.sendall(f"{len(st['files'].get(cmd[8:], b''))}\n".encode())
        elif cmd.startswith("cat ") and cmd.endswith("/appinfo.json"):
            if st["installed"]:
                chan.sendall(json.dumps({"id": lg_tv.APP_ID, "version": st["installed"]}).encode())
            else:
                chan.sendall(b"cat: can't open: No such file or directory\n")
                code = 1
        elif cmd == f"cat {lg_tv.SESSION_TOKEN_FILE}":
            chan.sendall(b"0123456789ABCDEF\n")
        elif "dev/install" in cmd:
            req = json.loads(re.search(r"'(\{.*\})'$", cmd).group(1))
            ipk = st["files"].get(req["ipkUrl"], b"")
            chan.sendall(b'{"subscribed":true,"returnValue":true}\n')
            chan.sendall(b'{"id":"com.dilmac.tv","details":{"state":"installing","progress":50},"returnValue":true}\n')
            if ipk.startswith(b"!<arch>\n"):
                st["installed"] = st["version"]
                chan.sendall(b'{"id":"com.dilmac.tv","details":{"state":"installed"},"returnValue":true}\n')
            else:
                chan.sendall(b'{"details":{"state":"install failed","errorCode":-1},"returnValue":true}\n')
            # luna-send-pub -i keeps running until the client closes the channel.
            while not chan.closed:
                if chan.recv_ready() and not chan.recv(1024):
                    break
                chan.status_event.wait(0.1)
            return
        elif "applicationManager/launch" in cmd:
            st["launched"] = json.loads(re.search(r"'(\{.*\})'$", cmd).group(1))
            chan.sendall(b'{"returnValue":true,"processId":"1001"}\n')
        elif cmd.startswith(("mkdir ", "rm ")):
            pass
        else:
            chan.sendall(f"sh: {cmd.split()[0]}: not found\n".encode())
            code = 127
        chan.send_exit_status(code)
        chan.close()


class LgSshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = {"commands": [], "files": {}, "installed": None, "version": lg_tv.web_version(), "launched": None}
        host_key = paramiko.RSAKey.generate(2048)
        client_key = paramiko.RSAKey.generate(2048)
        pem = io.StringIO()
        client_key.write_private_key(pem)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.transports = []

        def serve():
            while True:
                try:
                    conn, _ = self.sock.accept()
                except OSError:
                    return
                t = paramiko.Transport(conn)
                t.add_server_key(host_key)
                t.start_server(server=FakeTv(self.state))
                self.transports.append(t)

        threading.Thread(target=serve, daemon=True).start()
        self.patches = [mock.patch.object(paths, "LG_FILE", Path(self.tmp.name) / "lg-tv.json"),
                        mock.patch.object(lg_tv, "SSH_PORT", self.port),
                        mock.patch("tv_web.tv_token", return_value="tv-key-123")]
        for p in self.patches:
            p.start()
        lg_tv.save_state({"ip": "127.0.0.1", "key": pem.getvalue(), "passphrase": ""})

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.sock.close()
        for t in self.transports:
            t.close()
        self.tmp.cleanup()

    def open_app(self, answer=b'{"result":"success","errorCode":"200"}'):
        lines = []

        class Answer(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=Answer(answer)) as get:
            ok = lg_tv.open_app("127.0.0.1", 8788, 2, lines.append, key="tv-key-123")
        return ok, lines, get

    def test_first_start_installs_and_opens(self):
        ok, lines, get = self.open_app()
        self.assertTrue(ok, lines)
        self.assertEqual(self.state["installed"], lg_tv.web_version())
        self.assertEqual(self.state["launched"], {"id": lg_tv.APP_ID,
                                                  "params": {"host": "127.0.0.1", "port": 8788, "hdmi": 2,
                                                             "key": "tv-key-123"}})
        # the package went through cat (no SFTP) and was removed afterwards
        self.assertTrue(any(c.startswith("cat > /media/developer/temp/") for c in self.state["commands"]))
        self.assertTrue(any(c.startswith("rm -f /media/developer/temp/") for c in self.state["commands"]))
        # the session was renewed with the TV's token
        self.assertTrue(get.call_args[0][0].endswith("sessionToken=0123456789ABCDEF"))
        self.assertGreater(lg_tv.load_state().get("extended_at", 0), 0)
        self.assertTrue(any("açıldı" in line for line in lines))

    def test_same_version_only_opens(self):
        self.state["installed"] = lg_tv.web_version()
        lg_tv.save_state({**lg_tv.load_state(), "extended_at": 9_999_999_999})
        ok, lines, get = self.open_app()
        self.assertTrue(ok, lines)
        self.assertFalse(any("dev/install" in c for c in self.state["commands"]))
        get.assert_not_called()  # renewed recently

    def test_old_version_is_updated(self):
        self.state["installed"] = "0.0.1"
        ok, lines, _ = self.open_app()
        self.assertTrue(ok, lines)
        self.assertEqual(self.state["installed"], lg_tv.web_version())
        self.assertTrue(any("güncelleniyor" in line for line in lines))

    def test_failed_renewal_still_opens(self):
        ok, lines, _ = self.open_app(b'{"result":"fail","errorMsg":"expired"}')
        self.assertTrue(ok, lines)
        self.assertTrue(any("uzatılamadı" in line for line in lines))
        self.assertIsNotNone(self.state["launched"])

    def test_tv_identity_is_pinned(self):
        self.state["installed"] = lg_tv.web_version()
        ok, lines, _ = self.open_app()
        self.assertTrue(ok, lines)
        pinned = lg_tv.load_state()["host_key"]
        self.assertTrue(pinned.startswith("ssh-rsa "))
        ok, lines, _ = self.open_app()  # same TV: still fine
        self.assertTrue(ok, lines)
        # A device with another host key at the TV's address: refused before logging in.
        impostor = paramiko.RSAKey.generate(2048)
        lg_tv.save_state({**lg_tv.load_state(), "host_key": f"ssh-rsa {impostor.get_base64()}"})
        launched_before = self.state["launched"]
        ok, lines, _ = self.open_app()
        self.assertFalse(ok)
        self.assertTrue(any("kimliği" in line for line in lines), lines)
        self.assertIs(self.state["launched"], launched_before)

    def test_no_tv(self):
        with mock.patch.object(lg_tv, "SSH_PORT", 1):
            ok, lines, _ = self.open_app()
        self.assertFalse(ok)
        self.assertTrue(any("bağlanılamadı" in line for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
