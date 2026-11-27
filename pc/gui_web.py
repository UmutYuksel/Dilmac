"""Dilmaç - desktop GUI as an HTML page (pc/ui/) in a native window (pywebview + Edge WebView2).

The page has three parts: a setup wizard on first start (computer, PS5, TV, subtitle area, test), the main screen
(start/stop, PS5 / TV / translation cards, live subtitles, problems with fix buttons) and the settings page.

Python side:
  Api      the page calls its methods (window.pywebview.api.*); each call runs on its own thread, so a call may
           block (scanning the network, installing the TV app, waiting for a sign-in). Results are plain JSON.
  Bridge   events to the page: log lines, connection statuses, read/translated lines, install progress. They are
           batched every 80 ms into one py.events([...]) call.
  Runner   the translator (subtitler.run) on its own thread with its own asyncio loop.
  checker  a background thread that checks the connections every few seconds (checks.py).
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import queue
import threading
import time
import traceback
import webbrowser

import checks as B
import paths
import subtitler as A
from translation import Glossary

UI_DIR = paths.BUNDLE / "ui"
DEFAULT_REGION = dict(A.DEFAULTS["region"])
REMOTE_PLAY_PAGE = "https://remoteplay.dl.playstation.net/remoteplay/lang/tr/index.html"
VERSION = "0.8"
TV_KINDS = ("android", "lg", "samsung")
TV_LABELS = {"android": "Android TV", "lg": "LG", "samsung": "Samsung"}

# Settings the page may change (anything else in config.json stays as it is).
SETTING_KEYS = {
    "game": str, "engine": str, "local_model": str, "gemini_api_key": str, "gemini_model": str,
    "tv_ip": str, "tv_type": str, "tv_hdmi": int, "ps5_ip": str, "connection": str,
    "font_px": int, "box_opacity": float, "fade_ms": int, "old_line_seconds": float, "linger_seconds": float,
    "show_objectives": bool, "split_blocks": bool, "speaker_colors": bool, "voice_recognition": bool,
    "save_frames": bool, "tv_name": str, "share_lines": bool,
}


def webview2_installed() -> bool:
    """Edge WebView2 runtime (part of Windows 11 and of current Windows 10 updates)."""
    import winreg

    key = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for root, path in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node" + key[8:]),
                       (winreg.HKEY_LOCAL_MACHINE, key), (winreg.HKEY_CURRENT_USER, key)):
        try:
            with winreg.OpenKey(root, path) as k:
                version, _ = winreg.QueryValueEx(k, "pv")
                if version and version != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def available() -> bool:
    try:
        import webview  # noqa: F401
    except ImportError:
        return False
    return (UI_DIR / "index.html").is_file() and webview2_installed()


def status_dict(s: B.Status) -> dict:
    return {"level": s.level, "txt": s.txt, "data": s.data_}


# ---------------------------------------------------------------- events to the page

class Bridge:
    def __init__(self):
        self.window = None
        self.q: queue.Queue = queue.Queue()
        self.ready = threading.Event()
        threading.Thread(target=self._flush, daemon=True, name="ui-events").start()

    def emit(self, event: str, **data) -> None:
        self.q.put({"event": event, **data})

    def _flush(self) -> None:
        while True:
            first = self.q.get()
            batch = [first]
            time.sleep(0.08)
            try:
                while len(batch) < 400:
                    batch.append(self.q.get_nowait())
            except queue.Empty:
                pass
            self.ready.wait()
            try:
                self.window.evaluate_js("window.py && py.events(" + json.dumps(batch, ensure_ascii=False,
                                                                                default=str) + ")")
            except Exception:
                pass


class Runner:
    """The translator on its own thread with its own asyncio loop."""

    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task | None = None
        self.on_stopped = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, cfg: dict) -> None:
        def run_():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop = loop
            self.task = loop.create_task(A.run(cfg, notify=lambda event, **d: self.bridge.emit(event, **d)))
            try:
                loop.run_until_complete(self.task)
            except asyncio.CancelledError:
                pass
            except A.ConfigError as e:
                self.bridge.emit("error", txt=str(e))
            except Exception as e:
                A.log("Beklenmeyen hata:\n" + traceback.format_exc())
                self.bridge.emit("error", txt=f"Beklenmeyen hata: {e}")
            finally:
                loop.close()
                self.loop = self.task = None
                self.bridge.emit("stopped")
                if self.on_stopped:
                    self.on_stopped()

        self.thread = threading.Thread(target=run_, daemon=True, name="translator")
        self.thread.start()

    def stop(self) -> None:
        loop, task = self.loop, self.task
        if loop is not None and task is not None:
            loop.call_soon_threadsafe(task.cancel)


# ---------------------------------------------------------------- the page's API

class Api:
    def __init__(self, bridge: Bridge, start_checker: bool = True):
        self._bridge = bridge
        self._runner = Runner(bridge)
        self._runner.on_stopped = self._after_stop
        self._restart = False
        self._closing = False
        self._statuses: dict[str, dict] = {}
        self._check_event = threading.Event()
        self._ps5_last = 0.0
        self._slow_last = 0.0
        self._frame = None          # the picture shown in the subtitle area editor (PIL image)
        self._login_stop = threading.Event()
        self._install_lock = threading.Lock()
        self._ocr_installing = False
        if start_checker:
            A.log_hook(lambda line: bridge.emit("log", line=line))
            threading.Thread(target=self._checker, daemon=True, name="checker").start()
            threading.Thread(target=self._sync_packs, daemon=True, name="pack-sync").start()

    # -------- state and settings

    def state(self) -> dict:
        cfg = A.load_config()
        return {
            "version": VERSION,
            "cfg": {k: cfg.get(k) for k in list(SETTING_KEYS) + ["region", "setup_done", "google_only"]},
            "font_px": A.font_px(cfg),
            "setup_done": bool(cfg.get("setup_done", bool(cfg.get("tv_ip")))),
            "running": self._runner.running,
            "statuses": self._statuses,
            "paired": self._paired(),
            "accounts": self._accounts(),
            "games": [g.game_title for g in Glossary.all_glossaries()],
            "default_region": DEFAULT_REGION,
            "pc_ip": self._pc_ip(cfg.get("tv_ip", "")),
            "tv_labels": TV_LABELS,
            "base_font_px": A.BASE_FONT_PX,
            "pack_server": self._server_on(cfg),
        }

    @staticmethod
    def _server_on(cfg: dict) -> bool:
        import pack_server

        return pack_server.settings(cfg) is not None

    def save_settings(self, new: dict, restart: bool = True) -> dict:
        """Merges the page's fields into config.json (only known keys, converted to their types)."""
        try:
            user = A.user_config()
        except Exception as e:
            return {"ok": False, "error": f"config.json okunamadı: {e}"}
        for key, value in (new or {}).items():
            kind = SETTING_KEYS.get(key)
            if kind is None:
                continue
            try:
                user[key] = kind(value) if kind is not bool else bool(value)
            except (TypeError, ValueError):
                return {"ok": False, "error": f"Geçersiz değer: {key}"}
        if user.get("tv_type") not in TV_KINDS:
            user["tv_type"] = "android"
        if "engine" in (new or {}):
            if new["engine"] not in ("opus", "local", "gemini", "google"):
                return {"ok": False, "error": f"Bilinmeyen çeviri motoru: {new['engine']}"}
            user["google_only"] = new["engine"] == "google"
            if new["engine"] == "google":
                user["engine"] = "opus"
        if "font_px" in user:
            user["font_px"] = max(16, min(80, int(user["font_px"])))
            user.pop("font_scale", None)
        if "box_opacity" in user:
            user["box_opacity"] = max(0.3, min(1.0, float(user["box_opacity"])))
        user["tv_hdmi"] = max(0, min(4, int(user.get("tv_hdmi") or 0)))
        A.save_config(user)
        self.check_now()
        restarted = False
        if restart and self._runner.running:
            self._restart, restarted = True, True
            self._runner.stop()
        return {"ok": True, "restarted": restarted, "state": self.state()}

    def _sync_packs(self) -> None:
        """New packs from the pack server at start (the translator checks again when it starts)."""
        import pack_server

        try:
            pack_server.sync_packs(A.load_config(), A.log)
        except Exception as e:
            A.log(f"Paket sunucusuna bakılamadı ({e}).")

    def ui_ready(self, page: str = "") -> None:
        A.log(f"Arayüz açıldı ({page or '?'}).")

    def finish_setup(self) -> dict:
        user = A.user_config()
        user["setup_done"] = True
        A.save_config(user)
        return self.state()

    # -------- translation

    def start(self) -> dict:
        cfg = A.load_config()
        if not cfg.get("tv_ip"):
            return {"ok": False, "error": "TV seçilmedi. Kurulumdaki TV adımından TV'ni bağla."}
        if self._ocr_installing:
            return {"ok": False, "error": "İngilizce yazı tanıma paketi hâlâ kuruluyor (birkaç dakika sürebilir). "
                                          "Bitince Yapılacaklar'daki satır yeşile döner; sonra başlat."}
        if not self._runner.running:
            self._runner.start(cfg)
        return {"ok": True}

    def stop(self) -> dict:
        self._restart = False
        self._runner.stop()
        return {"ok": True}

    def _after_stop(self) -> None:
        if self._restart and not self._closing:
            self._restart = False
            self._runner.start(A.load_config())
            self._bridge.emit("restarted")

    # -------- checks

    def check_now(self, ps5: bool = False, slow: bool = False) -> None:
        if ps5:
            self._ps5_last = 0.0
        if slow:
            self._slow_last = 0.0
        self._check_event.set()

    def _set_status(self, name: str, status: B.Status) -> None:
        d = status_dict(status)
        self._statuses[name] = d
        self._bridge.emit("status", name=name, **d)

    def _checker(self) -> None:
        def line(name, fn, *args):
            try:
                s = fn(*args)
            except Exception as e:
                s = B.Status("unknown", f"Denetlenemedi ({e})")
            self._set_status(name, s)

        while not self._closing:
            cfg = A.load_config()
            now = time.monotonic()
            slow = now - self._slow_last > 300
            line("rp", self._rp_status, cfg)
            line("engine", self._engine_status, cfg)
            if slow:
                self._slow_last = now
                if not self._ocr_installing:  # while installing, _watch_ocr_install owns this line
                    line("ocr", B.check_ocr)
            if not self._runner.running or "tv" not in self._statuses:
                line("tv", B.check_tv, cfg.get("tv_ip", ""), cfg.get("tv_type", "android"))
            if now - self._ps5_last > 15:
                self._ps5_last = now
                line("ps5", self._ps5_status, cfg)
            if slow:
                line("network", B.network_profile)
            self._check_event.wait(4)
            self._check_event.clear()

    def _ps5_status(self, cfg) -> B.Status:
        s = B.find_ps5(cfg.get("ps5_ip", ""))
        ip = s.data_.get("ip")
        if ip and ip != cfg.get("ps5_ip"):
            user = A.user_config()
            user["ps5_ip"] = ip
            A.save_config(user)
        s.data_["paired"] = self._paired()
        return s

    @staticmethod
    def _rp_status(cfg) -> B.Status:
        if cfg.get("connection") == "direct":
            return B.Status("ok", "Gerekmiyor: PS5'e doğrudan bağlanılıyor", {"window": True, "direct": True})
        found = A.find_window(cfg["window_titles"])
        if found:
            vr = A.video_rect(found[0])
            if vr is None:
                return B.Status("warn", "Pencere simge durumunda; açık ve görünür kalmalı", {"window": True})
            return B.Status("ok", f"Pencere açık · görüntü {vr[2]}×{vr[3]}", {"window": True})
        if B.remote_play_exe() is None:
            return B.Status("bad", "Remote Play kurulu değil", {"installed": False})
        return B.Status("bad", "Remote Play kapalı", {"installed": True})

    @staticmethod
    def _engine_status(cfg) -> B.Status:
        if cfg.get("google_only") or cfg.get("engine") == "google":
            return B.Status("warn", "Google Çeviri (ücretsiz ama daha düz çeviri)", {"kind": "google"})
        if cfg.get("engine") == "gemini":
            s = B.check_gemini(cfg.get("gemini_api_key", ""))
            s.data_["kind"] = "gemini"
            return s
        if cfg.get("engine") == "opus":
            import opus_mt

            if not opus_mt.available():
                return B.Status("bad", "Çeviri kitaplığı (CTranslate2) eksik", {"kind": "opus", "model": None})
            if opus_mt.installed():
                return B.Status("ok", "Bilgisayardaki çeviri modeli hazır (OPUS-MT, ücretsiz)",
                                {"kind": "opus", "model": True})
            return B.Status("warn", f"Çeviri modeli indirilmedi ({opus_mt.SIZE_TEXT}, bir kez)",
                            {"kind": "opus", "model": False})
        s = B.check_ollama(cfg.get("local_url", "http://127.0.0.1:11434"), cfg.get("local_model", ""))
        s.data_["kind"] = "local"
        return s

    def fix(self, action: str) -> dict:
        """Fix buttons (the page asks the user first where Windows will ask for admin rights)."""
        cfg = A.load_config()
        if action == "ollama_start":
            ok = B.start_ollama()
            threading.Timer(2.5, self.check_now).start()
            return {"ok": ok, "error": None if ok else "Ollama kurulu değil. ollama.com adresinden kurabilirsin."}
        if action == "ollama_download":
            def job():
                B.download_ollama(cfg.get("local_url", "http://127.0.0.1:11434"), cfg.get("local_model", ""),
                                  lambda m: self._set_status("engine", B.Status("warn", f"İndiriliyor… {m}",
                                                                                {"kind": "local", "busy": True})))
                self.check_now()
            threading.Thread(target=job, daemon=True).start()
            return {"ok": True}
        if action == "opus_download":
            import opus_mt

            def job():
                def progress(done, total):
                    pct = 100 * done / total if total else 0
                    self._set_status("engine", B.Status("busy", f"Çeviri modeli indiriliyor… %{pct:.0f}",
                                                        {"kind": "opus", "busy": True}))
                try:
                    opus_mt.download(progress)
                    A.log("Çeviri modeli indirildi (OPUS-MT).")
                except Exception as e:
                    self._set_status("engine", B.Status("bad", str(e), {"kind": "opus", "model": False}))
                    A.log(f"Çeviri modeli indirilemedi: {e}")
                    return
                self.check_now()
            threading.Thread(target=job, daemon=True, name="opus-download").start()
            return {"ok": True}
        if action == "ocr_install":
            if self._ocr_installing:
                return {"ok": True}
            proc = B.install_ocr()
            if proc is None:
                return {"ok": False, "error": "Kurulum başlatılamadı (yönetici izni verilmedi mi?)."}
            self._ocr_installing = True
            self._set_status("ocr", B.Status("busy", "Kuruluyor… (birkaç dakika sürebilir, açılan pencereyi kapatma)",
                                             {"busy": True}))
            threading.Thread(target=self._watch_ocr_install, args=(proc,), daemon=True, name="ocr-install").start()
            return {"ok": True}
        if action == "network_private":
            common = (self._statuses.get("network") or {}).get("data", {}).get("common", [])
            if common:
                B.make_network_private(common)
            threading.Timer(5, lambda: self.check_now(slow=True)).start()
            return {"ok": True}
        if action == "firewall":
            return {"ok": B.allow_web_tv_firewall(int(cfg.get("tv_web_port") or 8788))}
        if action == "remote_play_open":
            ok = B.open_remote_play()
            self.check_now()
            return {"ok": ok, "error": None if ok else "PS Remote Play kurulu değil."}
        if action == "remote_play_download":
            webbrowser.open(REMOTE_PLAY_PAGE)
            return {"ok": True}
        if action == "open_log":
            os.startfile(str(paths.LOG_FILE)) if paths.LOG_FILE.exists() else None
            return {"ok": True}
        return {"ok": False, "error": f"bilinmeyen işlem {action}"}

    def _watch_ocr_install(self, proc, limit_s: float = 30 * 60) -> None:
        """Follows the admin window until Windows lists the en-US OCR language, the window closes, or time runs out.
        The window stays open after a successful install (it waits for Enter), so success is read from Windows."""
        start = time.monotonic()
        installed = False
        try:
            while not self._closing and time.monotonic() - start < limit_s:
                if B.ocr_language_present():
                    installed = True
                    break
                if not proc.alive():
                    installed = bool(B.ocr_language_present())  # it may have finished just before closing
                    break
                time.sleep(4)
        finally:
            proc.close()
            self._ocr_installing = False
        s = B.check_ocr()
        if s.level != "ok":
            if installed:
                s = B.Status("warn", "Paket kuruldu; Dilmaç'ı kapatıp yeniden aç", {"restart": True})
            elif time.monotonic() - start >= limit_s:
                s = B.Status("bad", "Kurulum çok uzun sürdü; açılan penceredeki iletiye bak")
            else:
                s = B.Status("bad", "Kurulum bitmeden pencere kapandı; yeniden Kur'a bas")
        A.log(f"Yazı tanıma paketi kurulumu: {s.txt}")
        self._set_status("ocr", s)

    def open_url(self, url: str) -> None:
        if str(url).startswith(("https://", "http://")):
            webbrowser.open(url)

    # -------- PS5

    @staticmethod
    def _paired() -> str | None:
        try:
            import ps5_direct as D

            return D.paired_name()
        except Exception:
            return None

    @staticmethod
    def _accounts() -> list[str]:
        try:
            import ps5_direct as D

            return D.accounts()
        except Exception:
            return []

    def ps5_find(self) -> dict:
        s = self._ps5_status(A.load_config())
        self._ps5_last = time.monotonic()
        self._set_status("ps5", s)
        return status_dict(s)

    def psn_login(self) -> dict:
        """The Sony sign-in in a small Edge window; the redirect is caught by itself."""
        import ps5_direct as D
        import psn_login as L

        if L.find_browser() is None:
            return {"ok": False, "error": "Microsoft Edge bulunamadı."}
        self._login_stop.clear()
        try:
            address = L.sign_in(D.login_url(), D._REDIRECT, stop=self._login_stop)
            return {"ok": True, "name": D.add_account(address)}
        except L.LoginClosed:
            return {"ok": False, "error": "Giriş penceresi kapatıldı; yeniden dene."}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def pair_ps5(self, pin: str) -> dict:
        import ps5_direct as D

        cfg = A.load_config()
        ip = cfg.get("ps5_ip", "")
        if not ip:
            return {"ok": False, "error": "PS5 bulunamadı. PS5'i aç ve 'Yeniden ara'ya bas."}
        D.attach_log(A.log)
        try:
            name = D.pair(ip, str(pin).strip())
        except Exception as e:
            return {"ok": False, "error": str(e)}
        user = A.user_config()
        user["connection"] = "direct"
        new_ip = (D.paired_ps5() or {}).get("host")
        if new_ip:
            user["ps5_ip"] = new_ip
        A.save_config(user)
        self.check_now(ps5=True)
        return {"ok": True, "name": name}

    # -------- TV

    @staticmethod
    def _pc_ip(tv_ip: str) -> str:
        import tv_web

        return tv_web.local_address_for(tv_ip) if tv_ip else tv_web.local_address_for("192.168.1.1")

    def scan_tvs(self) -> list[dict]:
        rows = B.scan_tvs()
        for r in rows:
            if r.get("lg_dev"):
                r["state"] = "Geliştirici modu açık"
            elif r.get("lg"):
                r["state"] = "Geliştirici modu kapalı"
            elif r.get("samsung"):
                r["state"] = "Geliştirici modu açık" if r.get("samsung_dev") else "Geliştirici modu kapalı"
            elif r.get("uygulama"):
                r["state"] = "Dilmaç uygulaması açık"
            else:
                r["state"] = "Ağ hata ayıklaması açık"
            r["label"] = TV_LABELS[r["kind"]]
        return rows

    def tv_check(self, ip: str, kind: str) -> dict:
        """The TV step's live checklist for the chosen TV."""
        out = {"status": status_dict(B.check_tv(ip, kind)), "items": [], "pc_ip": self._pc_ip(ip)}
        items = out["items"]
        if kind == "samsung":
            import samsung_tv

            info = samsung_tv.fetch_info(ip, timeout=1.5)
            items.append({"ok": info is not None, "txt": "TV ağda bulundu" + (f": {info.get('name')}" if info and
                                                                              info.get("name") else "")})
            dev = info.get("developer_mode") if info else None
            items.append({"ok": dev is True, "txt": "Developer mode açık"})
            host = (info or {}).get("developer_ip")
            items.append({"ok": bool(host) and host == out["pc_ip"],
                          "txt": f"Host PC IP: {host or '—'} (bu bilgisayar {out['pc_ip']})"})
            items.append({"ok": samsung_tv.has_profile(), "txt": "Samsung sertifikası alındı, uygulama kuruldu"})
        elif kind == "lg":
            import lg_tv

            any_lg = any(B.port_open(ip, p, 0.5) for p in B.LG_PORTS) or B.port_open(ip, B.LG_DEV_PORT, 0.5)
            dev = B.port_open(ip, B.LG_DEV_PORT, 0.6)
            items.append({"ok": any_lg, "txt": "TV ağda bulundu"})
            items.append({"ok": dev, "txt": "Developer Mode açık (Dev Mode Status: ON)"})
            items.append({"ok": dev and B.port_open(ip, lg_tv.KEY_PORT, 0.5) or lg_tv.has_key(),
                          "txt": "Key Server açık" + (" (anahtar alındı)" if lg_tv.has_key() else "")})
            items.append({"ok": lg_tv.has_key(), "txt": "Uygulama kuruldu"})
        else:
            app = B.port_open(ip, B.TV_PORT, 0.6)
            adb = B.port_open(ip, B.ADB_PORT, 0.5)
            items.append({"ok": app or adb, "txt": "TV ağda bulundu"})
            items.append({"ok": adb or app, "txt": "Ağ hata ayıklaması (USB hata ayıklama) açık"})
            items.append({"ok": app, "txt": "Dilmaç uygulaması açık"})
        return out

    def select_tv(self, ip: str, kind: str, name: str = "") -> dict:
        return self.save_settings({"tv_ip": ip, "tv_type": kind if kind in TV_KINDS else "android",
                                   "tv_name": name}, restart=True)

    def install_tv(self, passphrase: str = "") -> dict:
        """Installs the TV app for the chosen TV; progress lines arrive as "install" events."""
        if not self._install_lock.acquire(blocking=False):
            return {"ok": False, "error": "Kurulum zaten sürüyor."}
        cfg = A.load_config()
        ip, kind = cfg.get("tv_ip", ""), cfg.get("tv_type", "android")

        def say(line: str) -> None:
            A.log(f"TV kurulumu: {line}")
            self._bridge.emit("install", line=line)

        try:
            if not ip:
                return {"ok": False, "error": "Önce bir TV seç."}
            if kind == "android":
                ok = B.install_tv(ip, say)
            else:
                port = int(cfg.get("tv_web_port") or 8788)
                try:
                    hub = A.web_hub(cfg)  # listening before the app opens on the TV
                except OSError as e:
                    return {"ok": False, "error": f"TV için port açılamadı: {e}"}
                if kind == "lg":
                    import lg_tv

                    ok = lg_tv.setup(ip, passphrase, port, int(cfg.get("tv_hdmi") or 0), say)
                else:
                    import samsung_tv

                    self._login_stop.clear()

                    def sign_in():
                        self._bridge.emit("install", line="Tarayıcıda Samsung giriş sayfası açıldı; giriş yap.",
                                          login=True)
                        try:
                            return samsung_tv.sign_in(webbrowser.open, timeout=600, stop=self._login_stop)
                        finally:
                            self._bridge.emit("install", login=False)

                    ok = samsung_tv.install(ip, port, say, sign_in)
                if ok:
                    say("TV'nin bu bilgisayara bağlanması bekleniyor…")
                    if hub.wait_client(30):
                        say("TV bağlandı.")
                    else:
                        say("Uygulama açıldı ama TV bu bilgisayara bağlanamadı. Windows güvenlik duvarı sorduysa izin "
                            "ver ya da 'Güvenlik duvarı izni'ne bas; ağ profili 'Özel' olmalı.")
                        ok = False
            self.check_now()
            return {"ok": bool(ok)}
        finally:
            self._install_lock.release()

    def cancel_login(self) -> None:
        self._login_stop.set()

    def test_tv(self, text: str = "", overrides: dict | None = None) -> dict:
        cfg = {**A.load_config(), **(overrides or {})}
        ip = cfg.get("tv_ip", "")
        txt = text or "Dilmaç: bağlantı tamam. Türkçe altyazı İngilizcenin tam üstünde görünecek."
        ok = B.send_tv_text(ip, txt, cfg)
        A.log(f"TV'ye deneme yazısı {'gönderildi' if ok else 'gönderilemedi'} ({ip}).")
        self.check_now()
        return {"ok": ok}

    # -------- subtitle area

    def _frame_reply(self) -> dict:
        im = self._frame.copy()
        im.thumbnail((1280, 720))
        out = io.BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=85)
        w, h = self._frame.size
        return {"ok": True, "img": "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii"),
                "w": w, "h": h}

    def grab_frame(self) -> dict:
        found = A.find_window(A.load_config()["window_titles"])
        if not found:
            return {"ok": False, "error": "Remote Play penceresi bulunamadı. Remote Play'i açıp PS5'e bağlan ya da "
                                          "bir ekran görüntüsü aç."}
        vr = A.video_rect(found[0])
        if vr is None:
            return {"ok": False, "error": "Remote Play penceresi simge durumunda; pencereyi aç."}
        x, y, w, h = vr
        try:
            self._frame = A.grab(found[0], {"left": x, "top": y, "width": w, "height": h})
        except Exception as e:
            return {"ok": False, "error": f"Görüntü alınamadı: {e}"}
        return self._frame_reply()

    def open_image(self) -> dict:
        import webview
        from PIL import Image

        window = self._bridge.window
        chosen = window.create_file_dialog(webview.FileDialog.OPEN,
                                           file_types=("Resim (*.png;*.jpg;*.jpeg;*.bmp)", "Hepsi (*.*)"))
        if not chosen:
            return {"ok": False}
        try:
            im = Image.open(chosen[0]).convert("RGB")
        except Exception as e:
            return {"ok": False, "error": f"Açılamadı: {e}"}
        x, y, w, h = A.fit_16_9(*im.size)
        self._frame = im.crop((x, y, x + w, y + h))
        return self._frame_reply()

    def read_region(self, region: dict) -> dict:
        import ocr as O

        if self._frame is None:
            got = self.grab_frame()
            if not got.get("ok"):
                return got
        cfg = A.load_config()
        cfg["region"] = {k: float(region[k]) for k in ("left", "top", "width", "height")}
        frame = self._frame
        try:
            w, h = frame.size
            box = A.subtitle_box((0, 0, w, h), cfg["region"])
            crop = frame.crop((box["left"], box["top"], box["left"] + box["width"], box["top"] + box["height"]))
            glossary, lexicon = A.make_parts(cfg)
            reader = O.Reader(A.make_ocr_engine(), lexicon, float(cfg.get("ocr_scale") or 0),
                              A.make_layout(cfg, glossary))
            r = asyncio.run(reader.read(crop, h))
        except Exception as e:
            return {"ok": False, "error": f"Okuma hatası: {e}"}
        if r.turkish:
            txt = f"Türkçe yazı (çevrilmez): {r.raw_}"
        elif r.txt:
            txt = f"Okunan: {r.txt}"
        else:
            txt = "Altyazı okunmadı" + (f" (ham: {r.raw_})" if r.raw_ else "")
        if r.objective:
            txt += f"\nGörev: {r.objective}"
        return {"ok": True, "txt": txt}

    def save_region(self, region: dict) -> dict:
        user = A.user_config()
        user["region"] = {k: round(float(region[k]), 3) for k in ("left", "top", "width", "height")}
        A.save_config(user)
        restarted = False
        if self._runner.running:
            self._restart, restarted = True, True
            self._runner.stop()
        return {"ok": True, "restarted": restarted}

    # -------- window

    def _close(self) -> None:
        self._closing = True
        self._login_stop.set()
        self._check_event.set()
        if self._runner.running:
            self._runner.stop()
            self._runner.thread.join(timeout=3)


def main(start: bool = False) -> None:
    import webview

    A.set_dpi_aware()
    bridge = Bridge()
    api = Api(bridge)
    window = webview.create_window("Dilmaç", url=(UI_DIR / "index.html").as_uri(), js_api=api,
                                   width=1280, height=840, min_size=(1040, 700), background_color="#121317")
    bridge.window = window

    def loaded():
        bridge.ready.set()
        if start:
            threading.Timer(1.0, lambda: bridge.emit("autostart")).start()

    window.events.loaded += loaded
    window.events.closing += lambda: api._close()
    webview.start(gui="edgechromium", debug=bool(os.environ.get("DILMAC_UI_DEBUG")))
