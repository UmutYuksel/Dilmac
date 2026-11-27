"""Direct connection to the PS5 without the Remote Play app: chiaki-ng's connection library (libchiaki, AGPL-3.0),
called through the small C bridge in pc/chiaki_bridge/ (chiaki_bridge.dll). This is the library CouchSubs uses;
it supports current PS5 firmware.

One-time pairing ("Eşle" in the PS5 row under Connections):
1. PSN login: login_url() opens in the browser and the user signs in with a second PSN account on Sony's own page.
   The browser ends up at remoteplay.dl.playstation.net/remoteplay/redirect?code=...; that address is pasted into the
   app and add_account() gets the account id. The password never reaches the app, and the one-time code given to
   Sony is not kept.
2. PIN: the 8-digit code from Settings > System > Remote Play > Link Device on the PS5 is passed to pair().
The pairing is kept in pc/ps5-pairing.json.

Each session: PS5Stream(host).start() wakes the PS5 from rest mode if needed and starts the stream. latest_frame()
returns the latest decoded frame (PIL). Game audio is received only if an audio buffer is given. No window is opened.
The PS5 accepts one Remote Play connection at a time, so this cannot connect while the official app is connected.
"""
from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path

import paths

PAIRING_FILE = paths.PAIRING_FILE
BRIDGE_DLL = "chiaki_bridge.dll"

# Remote Play's public client credentials (chiaki-ng uses them too).
_CLIENT = "ba495a24-818c-472b-b12d-ff231c1b5745"
_CLIENT_SECRET = "mvaiZkRsAsI1IBkY"
_REDIRECT = "https://remoteplay.dl.playstation.net/remoteplay/redirect"
_SCOPE = ("psn:clientapp referenceDataService:countryConfig.read pushNotification:webSocket.desktop.connect "
           "sessionManager:remotePlaySession.system.update")
_AUTH = "https://auth.api.sonyentertainmentnetwork.com/2.0/oauth/authorize"
_TOKEN = "https://auth.api.sonyentertainmentnetwork.com/2.0/oauth/token"

RESOLUTIONS = {"360p": 1, "540p": 2, "720p": 3, "1080p": 4}

_LOG = logging.getLogger("dilmac.direct")
_LOG.addHandler(logging.NullHandler())
_LOG.propagate = False  # chiaki's messages only go where attach_log() points them


class PS5Error(Exception):
    """An error to show to the user (in Turkish)."""


# ---------------------------------------------------------------- PSN login

def login_url() -> str:
    q = {
        "service_entity": "urn:service-entity:psn", "response_type": "code", "client_id": _CLIENT,
        "redirect_uri": _REDIRECT, "scope": _SCOPE, "request_locale": "en_US", "ui": "pr",
        "service_logo": "ps", "layout_type": "popup", "smcid": "remoteplay", "prompt": "always",
        "PlatformPrivacyWs1": "minimal",
    }
    return _AUTH + "?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


def code_from_redirect(txt: str) -> str:
    """The one-time code from the redirect address in the pasted text. Text copied from the address bar may have spaces
    or quotes around it."""
    i = (txt or "").find(_REDIRECT)
    if i < 0:
        raise PS5Error("Yapıştırılan metinde remoteplay.dl.playstation.net/remoteplay/redirect adresi yok. "
                             "Girişten sonra tarayıcının adres çubuğundaki adresin tamamını kopyala.")
    address = re.split(r"[\s\"'<>]", txt[i:], maxsplit=1)[0]
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(address).query)
    if q.get("code") and len(q["code"][0]) > 1:
        return q["code"][0]
    if q.get("error"):
        description = (q.get("error_description") or [""])[0]
        raise PS5Error(f"Sony girişi tamamlamadı: {q['error'][0]} {description}".strip())
    raise PS5Error("Adreste giriş kodu yok. Girişi yeniden yapıp yeni adresi yapıştır.")


def account_info(code: str, session=None) -> tuple[str, int]:
    """Exchanges the code at Sony for the account id: (PSN name, numeric account id). The token is not kept."""
    import requests

    http = session or requests
    client_auth = (_CLIENT, _CLIENT_SECRET)
    try:
        r = http.post(_TOKEN, auth=client_auth, timeout=15, data={
            "grant_type": "authorization_code", "code": code, "scope": _SCOPE, "redirect_uri": _REDIRECT})
        if r.status_code != 200:
            raise PS5Error(f"Sony kodu kabul etmedi (HTTP {r.status_code}). Kod tek kullanımlıktır ve çabuk "
                                 "eskir: girişi yeniden yapıp yeni adresi hemen yapıştır.")
        token = r.json().get("access_token")
        if not token:
            raise PS5Error("Sony'nin yanıtında erişim anahtarı yok.")
        r = http.get(f"{_TOKEN}/{urllib.parse.quote(token, safe='')}", auth=client_auth, timeout=15)
        if r.status_code != 200:
            raise PS5Error(f"Hesap bilgisi alınamadı (HTTP {r.status_code}).")
        token_info = r.json()
    except PS5Error:
        raise
    except Exception as e:  # network error, broken reply
        raise PS5Error(f"Sony'ye ulaşılamadı: {e}") from e
    try:
        account_no = int(str(token_info["user_id"]))
    except (KeyError, ValueError) as e:
        raise PS5Error("Sony'nin yanıtında hesap kimliği yok.") from e
    return str(token_info.get("online_id") or f"psn-{account_no}"), account_no


def rp_id(account_no: int) -> str:
    """The format Remote Play wants: base64 of the 8-byte little-endian id."""
    return base64.b64encode(account_no.to_bytes(8, "little")).decode()


# ---------------------------------------------------------------- pairing file
# {"account": {"name": "PSN name", "id": 123}, "ps5": {"<mac>": {"name", "host", "regist_key", "rp_key", "date"}}}

def _read(path_: Path = PAIRING_FILE) -> dict:
    """Encrypted for this Windows user (secure_store, DPAPI); an older plain file is encrypted on first read."""
    import secure_store

    try:
        data_ = secure_store.read_json(path_)
        return data_ if isinstance(data_, dict) else {}
    except (OSError, ValueError, secure_store.SecretError):
        return {}


def _write(data_: dict, path_: Path = PAIRING_FILE) -> None:
    import secure_store

    secure_store.write_json(path_, data_)


def add_account(pasted: str, path_: Path = PAIRING_FILE) -> str:
    """Adds the PSN account from the redirect address pasted from the browser; returns the PSN name."""
    name, account_no = account_info(code_from_redirect(pasted))
    data_ = _read(path_)
    data_["account"] = {"name": name, "id": account_no}
    _write(data_, path_)
    return name


def accounts(path_: Path = PAIRING_FILE) -> list[str]:
    h = _read(path_).get("account") or {}
    return [h["name"]] if h.get("name") else []


def paired_ps5(path_: Path = PAIRING_FILE) -> dict | None:
    """The most recently paired PS5 (without asking the PS5); None if there is none."""
    ps5 = _read(path_).get("ps5") or {}
    if not ps5:
        return None
    return max(ps5.values(), key=lambda p: p.get("date", ""))


def is_paired(path_: Path = PAIRING_FILE) -> bool:
    return paired_ps5(path_) is not None


def paired_name(path_: Path = PAIRING_FILE) -> str | None:
    """For the GUI: the paired PS5's name and account."""
    p = paired_ps5(path_)
    if p is None:
        return None
    account = accounts(path_)
    return f"{p.get('name') or 'PS5'}" + (f" · {account[0]}" if account else "")


# ---------------------------------------------------------------- chiaki bridge

def bridge_dir() -> Path | None:
    for d in (paths.BUNDLE / "chiaki", paths.DATA / "chiaki_bridge" / "bin"):
        if (d / BRIDGE_DLL).is_file():
            return d
    return None


_FRAME_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_int32,
                            ctypes.c_void_p)
_EVENT_CB = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)
_LOG_CB = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_char_p)
_AUDIO_CB = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32,
                           ctypes.c_void_p)
BRIDGE_VERSION = 3  # 3: English export names (kp_session_open etc.); 2: audio callback

_bridge = None
_bridge_lock = threading.Lock()
_log_target = None  # function that forwards chiaki's warnings and errors to the app log


_HEXDUMP = re.compile(r"^(offset\s+0\s+1\s+2|\s+[0-9a-f]+(\s+[0-9a-f]{2})+\s)", re.I)


def _chiaki_log(level: int, message: bytes) -> None:
    txt = (message or b"").decode("utf-8", "replace")
    _LOG.log(logging.ERROR if level & 1 else logging.WARNING if level & 2 else logging.INFO, txt)
    if _HEXDUMP.match(txt):
        return  # chiaki's raw packet dump (offset 0 1 2 ...): meaningless to the user
    if _log_target is not None and level & 3:
        try:
            _log_target(f"chiaki: {txt}")
        except Exception:
            pass


_log_cb = _LOG_CB(_chiaki_log)  # the ctypes callback must be kept alive as long as it is in use


def attach_log(fn) -> None:
    """Writes chiaki's warnings and errors (in English) to the app's log."""
    global _log_target
    _log_target = fn


def bridge():
    """chiaki_bridge.dll (loaded once)."""
    global _bridge
    with _bridge_lock:
        if _bridge is not None:
            return _bridge
        folder = bridge_dir()
        if folder is None:
            raise PS5Error("Doğrudan PS5 bağlantısının kütüphanesi (chiaki_bridge.dll) bulunamadı. "
                                 "Uygulamayı güncel exe ile aç.")
        os.add_dll_directory(str(folder))
        dll = ctypes.CDLL(str(folder / BRIDGE_DLL))
        dll.kp_version.restype = ctypes.c_int
        if dll.kp_version() != BRIDGE_VERSION:
            raise PS5Error(f"chiaki_bridge.dll sürümü uymuyor ({dll.kp_version()}, beklenen {BRIDGE_VERSION}). "
                                 "Uygulamayı güncel exe ile aç.")
        dll.kp_error_text.argtypes = [ctypes.c_int]
        dll.kp_error_text.restype = ctypes.c_char_p
        dll.kp_init.argtypes = [_LOG_CB, ctypes.c_uint]
        dll.kp_register.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_int,
                                ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
        dll.kp_wakeup.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        dll.kp_session_open.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, _FRAME_CB, _EVENT_CB, _AUDIO_CB, ctypes.c_void_p,
                                     ctypes.POINTER(ctypes.c_int)]
        dll.kp_session_open.restype = ctypes.c_void_p
        dll.kp_session_close.argtypes = [ctypes.c_void_p]
        dll.kp_request_keyframe.argtypes = [ctypes.c_void_p]
        err = dll.kp_init(_log_cb, 1 | 2 | 4)  # error, warning, info
        if err:
            raise PS5Error(f"chiaki başlatılamadı: {dll.kp_error_text(err).decode()}")
        _bridge = dll
        return dll


def error_text(code: int) -> str:
    try:
        return bridge().kp_error_text(code).decode()
    except Exception:
        return str(code)


def pair(host: str, pin: str, path_: Path = PAIRING_FILE) -> str:
    """Pairs with the PS5's "Link Device" code. The PS5 must be on (no pairing in rest mode). Returns the PS5's name."""
    import checks as B

    pin = re.sub(r"[\s\-.]", "", pin or "")
    if not re.fullmatch(r"\d{8}", pin):
        raise PS5Error("Kod 8 haneli olmalı (PS5: Ayarlar > Sistem > Uzaktan Oynatma > Cihazı Bağla).")
    data_ = _read(path_)
    account = data_.get("account") or {}
    if not account.get("id"):
        raise PS5Error("Önce PSN hesabıyla giriş yap.")
    status = B.find_ps5(host)
    if status.level == "bad":
        raise PS5Error(f"PS5'e ulaşılamadı ({host}). PS5 açık ve aynı ağda mı?")
    if "Bekleme" in status.txt:
        raise PS5Error("PS5 dinlenme modunda. Eşleme için PS5'i aç ve Cihazı Bağla ekranını göster.")
    host = status.data_.get("ip") or host  # if the IP changed, the address found on the network (DHCP)
    dll = bridge()
    regist_key, rp_key = ctypes.create_string_buffer(16), ctypes.create_string_buffer(16)
    mac, name = ctypes.create_string_buffer(6), ctypes.create_string_buffer(64)
    outcome = dll.kp_register(host.encode(), int(account["id"]).to_bytes(8, "little"), int(pin), 1, 30000,
                        regist_key, rp_key, mac, name, len(name))
    if outcome == 2:
        raise PS5Error("PS5 yanıt vermedi (zaman aşımı). Cihazı Bağla ekranı açık mı, IP adresi doğru mu?")
    if outcome >= 1000:
        raise PS5Error(f"Eşleme başlatılamadı: {error_text(outcome - 1000)}")
    if outcome != 0:
        raise PS5Error("PS5 eşlemeyi kabul etmedi. Kod yanlış ya da süresi dolmuş olabilir; ekrandaki yeni "
                             "kodu dene. Bu PSN hesabının PS5'te bir kullanıcısı olmalı.")
    mac_hex = mac.raw.hex()
    ps5_name = name.value.decode("utf-8", "replace") or status.data_.get("name") or "PS5"
    data_.setdefault("ps5", {})[mac_hex] = {
        "name": ps5_name, "host": host, "regist_key": regist_key.raw.hex(), "rp_key": rp_key.raw.hex(),
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write(data_, path_)
    return ps5_name


# ---------------------------------------------------------------- video decoding

class Decoder:
    """Decodes the H.264/H.265 frames from chiaki with PyAV; keeps the latest frame."""

    PADDING = bytes(64)

    def __init__(self, codec_id: str = "h264"):
        import av
        from av.codec.context import Flags, Flags2, ThreadType

        self._av = av
        ctx = av.codec.Codec(codec_id, "r").create()
        ctx.flags = Flags.low_delay
        ctx.flags2 = Flags2.fast
        ctx.thread_type = ThreadType.SLICE  # multithreading that does not hold frames back
        ctx.open()
        self._ctx = ctx
        self._lock = threading.Lock()
        self._last = None
        self._last_time = 0.0
        self.frame_count = 0
        self.garbled = 0

    def feed(self, data_: bytes) -> bool:
        """False: the frame could not be decoded (a new keyframe should be requested from the PS5)."""
        try:
            frames = self._ctx.decode(self._av.packet.Packet(data_ + self.PADDING))
        except self._av.error.InvalidDataError:
            self.garbled += 1
            return False
        if not frames:
            return True
        frame = frames[-1]
        if getattr(frame, "is_corrupt", False):
            self.garbled += 1
            return True
        with self._lock:
            self._last, self._last_time = frame, time.monotonic()
            self.frame_count += 1
        return True

    def last(self):
        with self._lock:
            return self._last, self._last_time


# ---------------------------------------------------------------- stream

class PS5Stream:
    """Video stream from the PS5 on its own thread. status: "connecting" | "connected" | "lost" | "stopped"."""

    def __init__(self, host: str, resolution: str = "1080p", log=print, path_: Path = PAIRING_FILE, audio=None):
        """audio: an object with .add(pcm, samples, channels, rate) such as AudioBuffer; if given, game audio is received
        too."""
        self.host, self.resolution, self.log, self.path_ = host, resolution, log, path_
        self.audio = audio
        self.status = "stopped"
        self.err = ""
        self._stop_event = threading.Event()
        self._ended = threading.Event()
        self._reason = ""
        self._thread: threading.Thread | None = None
        self._decoder: Decoder | None = None
        self._session_ptr = None
        # the ctypes callbacks must be kept alive as long as the object lives
        self._frame_cb = _FRAME_CB(self._on_frame)
        self._event_cb = _EVENT_CB(self._on_event)
        self._audio_cb = _AUDIO_CB(self._on_audio) if audio is not None else None

    # -------------------------------------------------------- called from outside

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self.status = "connecting"
        self._thread = threading.Thread(target=self._run, name="ps5-akis", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        self.status = "stopped"

    def latest_frame(self, max_age: float = 2.0):
        """The latest decoded frame (PIL RGB), or None (no stream or the frame is too old)."""
        if self._decoder is None:
            return None
        frame, ts = self._decoder.last()
        if frame is None or time.monotonic() - ts > max_age:
            return None
        return frame.to_image()

    # -------------------------------------------------------- called from chiaki's threads

    def _on_frame(self, buf, length, lost, user) -> int:
        decoder = self._decoder
        if decoder is None:
            return 1
        try:
            return 1 if decoder.feed(ctypes.string_at(buf, length)) else 0
        except Exception as e:  # no exception may escape a callback
            _LOG.warning("kare çözülemedi: %s", e)
            return 0

    def _on_audio(self, pcm, samples, channels, rate, user) -> None:
        try:
            self.audio.add(ctypes.string_at(pcm, samples * channels * 2), samples, channels, rate)
        except Exception as e:  # no exception may escape a callback
            _LOG.warning("ses alınamadı: %s", e)

    def _on_event(self, kind, code, txt, user) -> None:
        if kind == 1:
            self.status, self.err = "connected", ""
            self.log(f"PS5'e doğrudan bağlandı ({self.host}, {self.resolution}).")
        elif kind == 2:
            self._reason = (txt or b"").decode("utf-8", "replace") or f"kod {code}"
            self._ended.set()
        elif kind == 3:
            self._reason = ("PS5 bu kullanıcı için giriş kodu (passcode) istiyor. PS5'te ikinci hesabın giriş kodunu "
                           "kapat: Ayarlar > Kullanıcılar ve Hesaplar > Oturum Açma Ayarları.")
            self._ended.set()

    # -------------------------------------------------------- thread

    def _run(self) -> None:
        backoff = 3.0
        while not self._stop_event.is_set():
            try:
                self._session()
                backoff = 3.0
            except PS5Error as e:
                self.err = str(e)
                self.log(f"PS5 bağlantısı: {e}")
            except Exception as e:  # unexpected: log it and retry
                self.err = f"{type(e).__name__}: {e}"
                self.log(f"PS5 bağlantı hatası: {self.err}")
            if self._stop_event.is_set():
                break
            self.status = "lost"
            self._stop_event.wait(backoff)
            backoff = min(30.0, backoff * 2)
            self.status = "connecting"
        self.status = "stopped"

    def _wake(self, dll, p: dict) -> None:
        import checks as B

        status = B.find_ps5(self.host)
        if status.level == "bad" or status.data_.get("ip", self.host) != self.host:
            # The IP may have changed (the router can hand out another address each time): search the network.
            matches = B.find_ps5("")
            if matches.level != "bad" and matches.data_.get("ip"):
                self.log(f"PS5 {self.host} adresinde değil, {matches.data_['ip']} adresinde bulundu.")
                self.host, status = matches.data_["ip"], matches
        if status.level == "bad":
            raise PS5Error(f"PS5'e ulaşılamadı ({self.host}). PS5 açık ya da dinlenme modunda ve aynı ağda mı?")
        if "Bekleme" not in status.txt:
            return
        self.log("PS5 dinlenme modunda; uyandırılıyor...")
        dll.kp_wakeup(self.host.encode(), bytes.fromhex(p["regist_key"]), 1)
        end_time = time.monotonic() + 60
        while time.monotonic() < end_time and not self._stop_event.is_set():
            if "Bekleme" not in B.find_ps5(self.host).txt:
                time.sleep(3)  # let the boot screen come up
                return
            time.sleep(2)
        raise PS5Error("PS5 uyanmadı. PS5'te 'İnternete bağlı kal' ve 'PS5'i ağ üzerinden açmayı etkinleştir' "
                             "açık mı (Ayarlar > Sistem > Güç Tasarrufu > Dinlenme Modunda Kullanılabilen Özellikler)?")

    def _session(self) -> None:
        p = paired_ps5(self.path_)
        if p is None:
            raise PS5Error("PS5 eşlenmemiş. Bağlantılar'da PS5 satırındaki 'Eşle' ile eşle.")
        dll = bridge()
        self._wake(dll, p)
        if self._stop_event.is_set():
            return
        self._ended.clear()
        self._reason = ""
        self._decoder = Decoder("h264")
        err = ctypes.c_int(0)
        ptr = dll.kp_session_open(self.host.encode(), bytes.fromhex(p["regist_key"]), bytes.fromhex(p["rp_key"]), 1,
                               RESOLUTIONS.get(self.resolution, 4), 30, 0, self._frame_cb, self._event_cb,
                               self._audio_cb, None, ctypes.byref(err))
        if not ptr:
            raise PS5Error(f"PS5 oturumu açılamadı: {error_text(err.value)}")
        self._session_ptr = ptr
        last_request = 0.0
        try:
            while not self._stop_event.is_set() and not self._ended.wait(0.5):
                c = self._decoder
                # Connected but no frame decodes (the first keyframe was missed): ask for a new one now and then.
                if (self.status == "connected" and c is not None and c.frame_count == 0
                        and time.monotonic() - last_request > 3):
                    dll.kp_request_keyframe(ptr)
                    last_request = time.monotonic()
        finally:
            self._session_ptr = None
            dll.kp_session_close(ptr)  # waits until chiaki's threads have finished
        if not self._stop_event.is_set():
            reason = self._reason or "PS5 bağlantıyı kapattı"
            if "remote" in reason.lower() and "use" in reason.lower():
                reason += " (Remote Play uygulaması açıksa kapat)"
            raise PS5Error(f"Akış bitti: {reason}")
