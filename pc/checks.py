"""Connection and install checks (for the GUI): PS5, Remote Play, TV, translation engine, OCR package, network profile.

Every check is quick and never raises; it returns its result as a Status. Fixes that need admin rights (OCR package,
network profile) are put to the user through Windows' UAC prompt; nothing is done silently here.
"""
from __future__ import annotations

import concurrent.futures
import ctypes
import ipaddress
import json
import os
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import paths

# Subprocess without opening a window (no console window flashing)
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass
class Status:
    level: str  # ok | warn | bad | unknown
    txt: str
    data_: dict = field(default_factory=dict)


# ---------------------------------------------------------------- PS5

PS5_PORT = 9302
_SRCH = b"SRCH * HTTP/1.1\ndevice-discovery-protocol-version:00030010\n"


def _local_networks() -> list[ipaddress.IPv4Network]:
    """The PC's private (192.168/10/172.16) IPv4 networks, as /24."""
    addresses: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    try:  # default outgoing interface (no packet is sent)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.0.1", 9))
        addresses.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    networks = []
    for a in addresses:
        ip = ipaddress.IPv4Address(a)
        if ip.is_private and not ip.is_loopback and not str(ip).startswith("169.254."):
            networks.append(ipaddress.IPv4Network(f"{ip}/24", strict=False))
    return list(dict.fromkeys(networks))


def find_ps5(ip: str = "", duration: float = 1.2) -> Status:
    """PS5 discovery protocol (UDP 9302): is it on, in rest mode, its name and (if it reports it) the running game."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.2)
    dests = [ip] if ip else []
    dests += [str(n.broadcast_address) for n in _local_networks()] + ["255.255.255.255"]
    try:
        s.bind(("", 0))
        for h in dict.fromkeys(dests):
            try:
                s.sendto(_SRCH, (h, PS5_PORT))
            except OSError:
                pass
        end_time = time.monotonic() + duration
        while time.monotonic() < end_time:
            try:
                data, (address, _) = s.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            lines_ = data.decode("utf-8", "replace").splitlines()
            if not lines_ or not lines_[0].startswith("HTTP/1.1"):
                continue
            row = dict(l.split(":", 1) for l in lines_[1:] if ":" in l)
            if row.get("host-type", "PS5") not in ("PS5", ""):
                continue
            code = lines_[0].split(" ", 2)[1] if " " in lines_[0] else ""
            name = row.get("host-name", "PS5")
            game_title = row.get("running-app-name", "").strip()
            data_ = {"ip": address, "name": name, "game": game_title, "titleid": row.get("running-app-titleid", "").strip()}
            if code == "200":
                extra = f" · {game_title}" if game_title else ""
                return Status("ok", f"Açık · {name} ({address}){extra}", data_)
            if code == "620":
                return Status("warn", f"Bekleme modunda · {name} ({address})", data_)
            return Status("warn", f"{name} ({address}) yanıt verdi: {lines_[0]}", data_)
    finally:
        s.close()
    return Status("bad", "Bulunamadı (kapalı mı, aynı ağda mı?)")


# ---------------------------------------------------------------- TV

TV_PORT = 8787
ADB_PORT = 5555


def port_open(ip: str, port: int, duration: float = 0.6) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=duration):
            return True
    except OSError:
        return False


LG_DEV_PORT = 9922   # LG developer mode SSH
LG_PORTS = (3000, 3001)  # LG's remote control service (every LG webOS TV)


def check_tv(ip: str, kind: str = "android") -> Status:
    if not ip:
        return Status("bad", "TV IP adresi girilmedi")
    if kind == "lg":
        return check_lg_tv(ip)
    if kind == "samsung":
        return check_samsung_tv(ip)
    if port_open(ip, TV_PORT):
        return Status("ok", f"Bağlanabilir ({ip})", {"ip": ip})
    if port_open(ip, ADB_PORT, 0.4):
        return Status("warn", f"TV açık ama uygulama yanıt vermiyor ({ip}); TV'de uygulamayı aç ya da kur", {"ip": ip})
    return Status("bad", f"Ulaşılamıyor ({ip}); TV açık ve aynı ağda mı?", {"ip": ip})


def check_lg_tv(ip: str) -> Status:
    import lg_tv
    import tv_web

    hub = tv_web.existing_hub()
    if hub is not None and hub.clients():
        return Status("ok", f"Bağlı · LG TV'de uygulama açık ({hub.last_tv.get('ip') or ip})", {"ip": ip})
    if port_open(ip, LG_DEV_PORT):
        if lg_tv.has_key():
            return Status("ok", f"LG geliştirici modu açık ({ip}) · uygulama çeviri başlayınca açılır",
                          {"ip": ip, "lg_ready": True})
        return Status("warn", f"LG geliştirici modu açık ({ip}) · uygulama kurulmadı, 'Kur'a bas", {"ip": ip})
    if any(port_open(ip, p, 0.4) for p in LG_PORTS):
        return Status("bad", f"LG TV açık ({ip}) ama geliştirici modu kapalı ya da süresi dolmuş · 'Kur'daki adımlar",
                      {"ip": ip})
    return Status("bad", f"Ulaşılamıyor ({ip}); TV açık ve aynı ağda mı?", {"ip": ip})


def check_samsung_tv(ip: str) -> Status:
    import samsung_tv
    import tv_web

    hub = tv_web.existing_hub()
    if hub is not None and hub.clients():
        return Status("ok", f"Bağlı · Samsung TV'de uygulama açık ({hub.last_tv.get('ip') or ip})", {"ip": ip})
    info = samsung_tv.fetch_info(ip, timeout=1.2)
    if info is None:
        return Status("bad", f"Ulaşılamıyor ({ip}); TV açık ve aynı ağda mı?", {"ip": ip})
    pc = tv_web.local_address_for(ip)
    name = info.get("name") or "Samsung TV"
    if info.get("developer_mode") is False:
        return Status("bad", f"{name} ({ip}): geliştirici modu kapalı · 'Kur'daki adımlar", {"ip": ip})
    if info.get("developer_ip") and pc and info["developer_ip"] != pc:
        return Status("bad", f"{name}: TV'deki Host PC IP {info['developer_ip']}, bu bilgisayar {pc} · 'Kur'daki adımlar",
                      {"ip": ip})
    if not samsung_tv.has_profile():
        return Status("warn", f"{name} ({ip}) · uygulama kurulmadı, 'Kur'a bas", {"ip": ip})
    return Status("ok", f"{name} ({ip}) · uygulama çeviri başlayınca açılır", {"ip": ip})


SAMSUNG_PORT = 8001  # Samsung's TV info / remote control service (every Samsung smart TV)


def scan_tvs() -> list[dict]:
    """Looks on the local network for the Android TV app (8787), Android devices with network debugging on (5555),
    LG webOS TVs (developer mode 9922, remote control service 3000/3001) and Samsung TVs (8001)."""
    candidates = [str(h) for n in _local_networks() for h in n.hosts()]

    def probe(ip):
        app = port_open(ip, TV_PORT, 0.35)
        adb = not app and port_open(ip, ADB_PORT, 0.35)
        lg_dev = not (app or adb) and port_open(ip, LG_DEV_PORT, 0.35)
        lg = lg_dev or (not (app or adb) and any(port_open(ip, p, 0.35) for p in LG_PORTS))
        samsung = not (app or adb or lg) and port_open(ip, SAMSUNG_PORT, 0.35)
        if not (app or adb or lg or samsung):
            return None
        row = {"ip": ip, "uygulama": app, "adb": adb, "lg": lg, "lg_dev": lg_dev, "samsung": samsung,
               "kind": "lg" if lg else "samsung" if samsung else "android"}
        if samsung:
            import samsung_tv

            info = samsung_tv.fetch_info(ip, timeout=1.5) or {}
            row.update(name=info.get("name") or "", samsung_dev=bool(info.get("developer_mode")))
        return row

    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as ex:
        matches = [r for r in ex.map(probe, candidates) if r]
    return sorted(matches, key=lambda r: (not (r["uygulama"] or r["lg_dev"] or r.get("samsung_dev")),
                                          not (r["lg"] or r["samsung"]), ipaddress.IPv4Address(r["ip"])))


def send_tv_text(ip: str, txt: str, cfg: dict | None = None) -> bool:
    """Test caption: where a single-row GTA subtitle sits (60 dp from the bottom if the TV app is old)."""
    import subtitler as A  # imported here to avoid a circular import

    cfg = cfg or {}
    msg = {"type": "show", "id": "speech", "text": txt, "ttl": 6, "new": False,
           "size": int(cfg.get("font_size", 30)), "bottom": int(cfg.get("bottom_margin", 60)),
           "box": [0.33, 0.886, 0.34, 0.029], "lines": 1, "lineh": A.line_height(cfg),
           "opacity": float(cfg.get("box_opacity", 0.9)), "scale": 1.0,
           "max_width": float(cfg.get("box_width", 0.8)), "fade_ms": int(cfg.get("fade_ms", 180))}
    if cfg.get("tv_type", "android") in A.WEB_TVS:
        return send_web_tv(cfg, msg)
    try:
        with socket.create_connection((ip, TV_PORT), timeout=2) as s:
            s.sendall((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        return True
    except OSError:
        return False


def send_web_tv(cfg: dict, msg: dict, wait: float = 25) -> bool:
    """A message to the TV web app (LG / Samsung): starts the server and opens the app on the TV first if no page is
    connected."""
    import subtitler as A

    try:
        hub = A.web_hub(cfg)
    except OSError as e:
        A.log(f"TV için port açılamadı: {e}")
        return False
    if not hub.clients():
        if not A.open_web_tv_app(cfg):
            return False
        if not hub.wait_client(wait):
            A.log("TV uygulaması açıldı ama bu bilgisayara bağlanmadı. Windows güvenlik duvarı izin istediyse izin ver; "
                  "ağ profili 'Özel' olmalı. TV'de OK tuşuna basınca bağlantı bilgisi görünür.")
            return False
        time.sleep(0.5)  # the page applies the hello message first
    return hub.broadcast(msg) > 0


# The app was called "PS5 Altyazı" before 2026-11-26. The old TV app also listens on 8787, so it is removed when the
# new one is installed; the old firewall rule is replaced by the new one.
OLD_TV_PACKAGE = "com.ps5altyazi.tv"
OLD_FIREWALL_RULE = "PS5 Altyazi TV"


def allow_web_tv_firewall(port: int) -> bool:
    """Lets the TV reach this PC's subtitle server on a private network (Windows asks for admin rights)."""
    rule = "Dilmac TV"
    return as_admin(f"netsh advfirewall firewall delete rule name='{OLD_FIREWALL_RULE}' | Out-Null; "
                    f"netsh advfirewall firewall delete rule name='{rule}' | Out-Null; "
                    f"netsh advfirewall firewall add rule name='{rule}' dir=in action=allow protocol=TCP "
                    f"localport={int(port)} profile=private,domain")


def adb_path() -> Path | None:
    """adb shipped inside the exe first, then the Android SDK, then PATH."""
    import shutil

    candidates = [paths.BUNDLE / "adb" / "adb.exe"]
    for root in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                 str(Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk")):
        if root:
            candidates.append(Path(root) / "platform-tools" / "adb.exe")
    found = shutil.which("adb")
    if found:
        candidates.append(Path(found))
    for p in candidates:
        if p.is_file():
            return p
    return None


def install_tv(ip: str, log) -> bool:
    """Installs the TV app over the network (the same steps as tv/install.ps1). log: progress line by line."""
    adb, apk = adb_path(), paths.apk()
    if adb is None:
        log("adb bulunamadı (Android SDK platform-tools). Kurulum yapılamıyor.")
        return False
    if apk is None:
        log("dilmac-tv.apk bulunamadı.")
        return False
    dest = ip if ":" in ip else f"{ip}:{ADB_PORT}"
    pkg = "com.dilmac.tv"

    def run_adb(*args, duration=60):
        r = subprocess.run([str(adb), *args], capture_output=True, text=True, timeout=duration, creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL,
                           encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout + r.stderr).strip()

    try:
        log(f"TV'ye bağlanılıyor: {dest}. TV ekranında 'hata ayıklamaya izin ver' sorusu çıkarsa kabul et.")
        run_adb("connect", dest, duration=15)
        status = ""
        for i in range(30):
            _, status = run_adb("-s", dest, "get-state", duration=10)
            if status == "device":
                break
            if i == 0:
                log("TV'deki onay penceresini bekliyorum (60 sn)...")
            time.sleep(2)
            if i % 5 == 4:
                run_adb("connect", dest, duration=15)
        if status != "device":
            log("TV bağlanmadı. TV'de Geliştirici seçenekleri → USB hata ayıklama açık mı? Onay penceresine bak.")
            return False
        code, output = run_adb("-s", dest, "shell", "pm", "list", "packages", OLD_TV_PACKAGE, duration=15)
        if code == 0 and f"package:{OLD_TV_PACKAGE}" in output.split():
            log("Eski adlı TV uygulaması (PS5 Altyazı) kaldırılıyor; yenisi aynı portu kullanıyor.")
            run_adb("-s", dest, "uninstall", OLD_TV_PACKAGE, duration=60)
        log("Uygulama kuruluyor...")
        code, output = run_adb("-s", dest, "install", "-r", str(apk), duration=120)
        if code != 0:
            log(f"Kurulum başarısız: {output[-300:]}")
            return False
        run_adb("-s", dest, "shell", "appops", "set", pkg, "SYSTEM_ALERT_WINDOW", "allow")
        # Starting the app also starts the subtitle service (an update alone does not always restart it).
        run_adb("-s", dest, "shell", "am", "start", "-n", f"{pkg}/.MainActivity", "--ez", "test", "true")
        log("Kuruldu. TV'de 'Dilmaç' ekranı açılmalı ve bir deneme altyazısı görünmeli.")
        return True
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"adb çalıştırılamadı: {e}")
        return False
    finally:
        if adb.parent == paths.BUNDLE / "adb":
            # The adb server started from the exe's temp folder would keep it locked after the app closes.
            try:
                run_adb("kill-server", duration=10)
            except (OSError, subprocess.TimeoutExpired):
                pass


# ---------------------------------------------------------------- Remote Play

def remote_play_exe() -> Path | None:
    for root in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if root:
            p = Path(root) / "Sony" / "PS Remote Play" / "RemotePlay.exe"
            if p.is_file():
                return p
    return None


def open_remote_play() -> bool:
    p = remote_play_exe()
    if p is None:
        return False
    os.startfile(str(p))
    return True


# ---------------------------------------------------------------- translation engine

def check_ollama(url: str, model: str) -> Status:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=1.5) as r:
            models = [m.get("name", "") for m in json.loads(r.read().decode("utf-8")).get("models", [])]
    except Exception:
        return Status("bad", "Ollama çalışmıyor", {"running": False})
    if model in models or f"{model}:latest" in models:
        return Status("ok", f"Yerel model hazır ({model})", {"running": True, "model": True})
    return Status("warn", f"Ollama açık ama '{model}' modeli yok", {"running": True, "model": False})


def start_ollama() -> bool:
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama"
    for name in ("ollama app.exe", "ollama.exe"):
        p = root / name
        if p.is_file():
            if name == "ollama.exe":
                subprocess.Popen([str(p), "serve"], creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.startfile(str(p))
            return True
    return False


def download_ollama(url: str, model: str, progress) -> bool:
    """Downloads the model (a large file). Reports progress through progress(text)."""
    req = urllib.request.Request(url.rstrip("/") + "/api/pull", data=json.dumps({"model": model}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    last = 0.0
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            for line_count in r:
                d = json.loads(line_count.decode("utf-8") or "{}")
                if d.get("error"):
                    progress(f"Hata: {d['error']}")
                    return False
                if time.monotonic() - last > 0.5 or d.get("status") == "success":
                    last = time.monotonic()
                    if d.get("total"):
                        progress(f"{d.get('status', '')}: %{100 * d.get('completed', 0) / d['total']:.0f}")
                    else:
                        progress(d.get("status", ""))
        return True
    except Exception as e:
        progress(f"İndirilemedi: {e}")
        return False


def check_gemini(text_key: str) -> Status:
    if not text_key:
        return Status("bad", "Gemini anahtarı girilmedi")
    return Status("ok", "Gemini anahtarı girildi")


# ---------------------------------------------------------------- OCR package and network profile

OCR_COMMAND = 'Add-WindowsCapability -Online -Name "Language.OCR~~~en-US~0.0.1.0"'


def ocr_language_present() -> bool | None:
    """Does Windows list en-US among its OCR languages. Asked in a new PowerShell process each time, so a pack
    installed after this program started is seen too. None: could not be asked."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null; "
             "[Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag }"],
            capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL,
            encoding="utf-8", errors="replace")
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return any(t.strip().lower() == "en-us" for t in r.stdout.splitlines())


def check_ocr() -> Status:
    try:
        import ocr

        if ocr.ocr_engine() is not None:
            return Status("ok", "İngilizce yazı tanıma kurulu")
    except Exception as e:
        return Status("bad", f"Yazı tanıma açılamadı ({e})")
    return Status("bad", "İngilizce yazı tanıma paketi kurulu değil")


def network_profile() -> Status:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetConnectionProfile | Select-Object InterfaceAlias,NetworkCategory | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
        data = json.loads(r.stdout or "[]")
    except Exception:
        return Status("unknown", "Ağ profili okunamadı")
    if isinstance(data, dict):
        data = [data]
    # NetworkCategory: 0 Public, 1 Private, 2 DomainAuthenticated (or as text)
    common = [d["InterfaceAlias"] for d in data if str(d.get("NetworkCategory")) in ("0", "Public")]
    if common:
        return Status("warn", f"Ortak ağ: {', '.join(common)} (PS5 ve TV bulunamayabilir)", {"common": common})
    return Status("ok", "Özel ağ", {"common": []})


def as_admin(cmd: str) -> bool:
    """Opens a PowerShell command as admin (Windows UAC asks). When the command finishes the window closes on a key press."""
    full_ = f"{cmd}; Write-Host ''; Read-Host 'Bitti. Kapatmak için Enter'"
    r = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe",
                                            f'-NoProfile -ExecutionPolicy Bypass -Command "{full_}"', None, 1)
    return r > 32


def make_network_private(interfaces: list[str]) -> bool:
    cmd = "; ".join(f"Set-NetConnectionProfile -InterfaceAlias '{a}' -NetworkCategory Private" for a in interfaces)
    return as_admin(cmd)


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("fMask", ctypes.c_ulong), ("hwnd", ctypes.c_void_p),
                ("lpVerb", ctypes.c_wchar_p), ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
                ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int), ("hInstApp", ctypes.c_void_p),
                ("lpIDList", ctypes.c_void_p), ("lpClass", ctypes.c_wchar_p), ("hkeyClass", ctypes.c_void_p),
                ("dwHotKey", ctypes.c_ulong), ("hIconOrMonitor", ctypes.c_void_p), ("hProcess", ctypes.c_void_p)]


class AdminProcess:
    """An admin PowerShell window started by us; tells whether it is still open."""

    def __init__(self, handle: int):
        self._h = handle

    def alive(self) -> bool:
        if not self._h:
            return False
        return ctypes.windll.kernel32.WaitForSingleObject(ctypes.c_void_p(self._h), 0) == 0x102  # WAIT_TIMEOUT

    def close(self) -> None:
        if self._h:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self._h))
            self._h = 0


def as_admin_process(cmd: str) -> AdminProcess | None:
    """Like as_admin, but keeps the process so its end can be followed. None: not started (e.g. UAC refused)."""
    full_ = f"{cmd}; Write-Host ''; Read-Host 'Bitti. Kapatmak için Enter'"
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = "powershell.exe"
    info.lpParameters = f'-NoProfile -ExecutionPolicy Bypass -Command "{full_}"'
    info.nShow = 1
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        return None
    return AdminProcess(info.hProcess or 0)


def install_ocr() -> AdminProcess | None:
    return as_admin_process(OCR_COMMAND.replace('"', "'"))
