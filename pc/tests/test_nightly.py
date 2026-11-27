"""The pool job's schedule (2026-11-27: every few minutes instead of once a night) and its quiet empty runs."""
import unittest
from pathlib import Path
from unittest import mock

import nightly as N


class TaskScriptTest(unittest.TestCase):
    def test_repeats_without_end_and_runs_quiet(self):
        ps = N.task_script(Path(r"C:\Py\python.exe"), Path(r"C:\Dilmac\pc\nightly.py"))
        self.assertIn(f"-RepetitionInterval (New-TimeSpan -Minutes {N.EVERY_MINUTES})", ps)
        self.assertNotIn("-RepetitionDuration", ps)  # no end date
        self.assertNotIn("-Daily", ps)
        self.assertIn("-MultipleInstances IgnoreNew", ps)  # a long run is not started twice
        self.assertIn(r"""-Argument '"C:\Dilmac\pc\nightly.py" --nightly --quiet'""", ps)
        self.assertIn(r"-WorkingDirectory 'C:\Py'", ps)
        self.assertLessEqual(N.EVERY_MINUTES, 15)

    def test_install_uses_pythonw(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            for name in ("python.exe", "pythonw.exe"):
                (Path(d) / name).write_bytes(b"")
            with mock.patch.object(N.subprocess, "run") as run:
                run.return_value.returncode = 0
                self.assertTrue(N.install_task(Path(d) / "python.exe"))
            ps = run.call_args[0][0][-1]
            self.assertIn(f"-Execute '{Path(d) / 'pythonw.exe'}'", ps)

    def test_frozen_exe_gets_flags_only(self):
        ps = N.task_script(Path(r"C:\Dilmac\Dilmac.exe"), None)
        self.assertIn("-Argument '--nightly --quiet'", ps)


class QuietRunTest(unittest.TestCase):
    def run_with(self, waiting, quiet):
        logs = []
        with mock.patch.object(N.S, "sync_packs", return_value={}), \
                mock.patch.object(N.S, "admin_lines", return_value=waiting), \
                mock.patch.object(N, "_wait_engine", return_value=False):
            result = N.run({}, logs.append, quiet=quiet)
        return result, logs

    def test_empty_pool_logs_nothing_when_quiet(self):
        self.assertEqual(self.run_with([], quiet=True), ({}, []))
        self.assertEqual(self.run_with([], quiet=False)[1], ["Havuzda bekleyen satır yok."])

    def test_waiting_lines_are_logged_even_when_quiet(self):
        _, logs = self.run_with([{"id": 1, "game": "gta vi", "en": "Hi there."}], quiet=True)
        self.assertEqual(logs, ["Havuzda 1 satır bekliyor; çeviri başladı."])

    def test_main_quiet_writes_nothing_for_an_empty_pool(self):
        import subtitler as A

        with mock.patch.object(A, "load_config", return_value={}), mock.patch.object(A, "log") as log, \
                mock.patch.object(N, "run", return_value={}), mock.patch.object(N, "merge_voices", return_value={}):
            N.main(["--nightly", "--quiet"])
            log.assert_not_called()
            N.main(["--nightly"])
            self.assertEqual(log.call_count, 3)  # started, finished, the voice pool


if __name__ == "__main__":
    unittest.main()
