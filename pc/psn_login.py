"""Signing in to PSN in a small app window and catching Sony's redirect address automatically.

Microsoft Edge (or Chrome) is started in app mode (no address bar, a small window) with a fresh temporary profile and a
local DevTools port. The page is Sony's own; the password goes to Sony only. The app only reads the addresses of the
open pages (http://127.0.0.1:<port>/json/list) to catch the redirect to remoteplay.dl.playstation.net/...?code=...,
then closes the window and deletes the temporary profile (no cookies are kept).

If no such browser is installed, the caller falls back to opening the system browser and pasting the address.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

PROFILE_PREFIX = "dilmac-psn-"


class LoginClosed(Exception):
    """The user closed the login window before signing in."""


def find_browser() -> Path | None:
    """Edge first (always present on Windows 10/11), then Chrome."""
    roots = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    names = [Path("Microsoft/Edge/Application/msedge.exe"), Path("Google/Chrome/Application/chrome.exe")]
    for name in names:
        for root in roots:
            if root and (Path(root) / name).is_file():
                return Path(root) / name
    return None


def find_redirect(targets: list[dict], prefix: str) -> str | None:
    """The address of an open page that is the redirect with a code, or None."""
    for t in targets:
        url = t.get("url") or ""
        if url.startswith(prefix) and "code=" in url:
            return url
    return None


def devtools_port(profile: Path) -> int | None:
    """The port the browser chose (--remote-debugging-port=0 writes it to DevToolsActivePort)."""
    try:
        first = (profile / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()[0]
        return int(first)
    except (OSError, ValueError, IndexError):
        return None


def clean_leftovers(base: Path | None = None) -> None:
    """Deletes temporary profiles a previous run could not remove (the browser still held files then)."""
    base = base or Path(tempfile.gettempdir())
    for d in base.glob(PROFILE_PREFIX + "*"):
        shutil.rmtree(d, ignore_errors=True)


class LoginWindow:
    def __init__(self, url: str, redirect_prefix: str, browser: Path | None = None, size=(520, 720)):
        self.url, self.prefix, self.size = url, redirect_prefix, size
        self.browser = browser or find_browser()
        self.profile: Path | None = None
        self.proc: subprocess.Popen | None = None
        self._seen_page = False

    def start(self) -> None:
        if self.browser is None:
            raise FileNotFoundError("Microsoft Edge ya da Google Chrome bulunamadı.")
        clean_leftovers()
        self.profile = Path(tempfile.mkdtemp(prefix=PROFILE_PREFIX))
        w, h = self.size
        args = [str(self.browser), f"--app={self.url}", f"--user-data-dir={self.profile}",
                "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1", f"--window-size={w},{h}",
                "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
                "--disable-features=msEdgeStartupBoost,Translate"]
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)

    def _targets(self) -> list[dict] | None:
        port = devtools_port(self.profile) if self.profile else None
        if port is None:
            return None
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
                return [t for t in json.loads(r.read().decode("utf-8")) if t.get("type") == "page"]
        except (OSError, ValueError):
            return None

    def poll(self) -> str | None:
        """The redirect address once Sony sends it; None while waiting. LoginClosed if the window was closed."""
        targets = self._targets()
        if targets:
            self._seen_page = True
            return find_redirect(targets, self.prefix)
        if self.proc is not None and self.proc.poll() is not None:
            raise LoginClosed()
        if self._seen_page and targets == []:
            raise LoginClosed()  # the browser lingers in the background but the window is gone
        return None

    def wait(self, timeout: float = 600, stop=None, interval: float = 0.4) -> str:
        """Blocks until the redirect address arrives (for a worker thread). stop: threading.Event to cancel."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if stop is not None and stop.is_set():
                raise LoginClosed()
            found = self.poll()
            if found:
                return found
            time.sleep(interval)
        raise TimeoutError("PSN girişi zaman aşımına uğradı.")

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.profile is not None:
            for _ in range(10):  # the browser's child processes may hold files for a moment
                shutil.rmtree(self.profile, ignore_errors=True)
                if not self.profile.exists():
                    break
                time.sleep(0.3)


def sign_in(url: str, redirect_prefix: str, timeout: float = 600, stop=None) -> str:
    """Opens the login window, returns the redirect address and closes the window again."""
    win = LoginWindow(url, redirect_prefix)
    win.start()
    try:
        return win.wait(timeout, stop)
    finally:
        win.close()
