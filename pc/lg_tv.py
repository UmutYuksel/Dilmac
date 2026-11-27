"""LG TV (webOS): installs and opens the TV web app (tv-web/) over the TV's developer mode.

What the user does once on the TV (the GUI's LG window shows these steps):
  1. a free account at webostv.developer.lge.com,
  2. the "Developer Mode" app from the LG Content Store, signed in with that account, "Dev Mode Status" on (the TV
     restarts),
  3. "Key Server" on in the same app, and the 6-character passphrase it shows typed into the PC.

Then the PC:
  - downloads the TV's SSH key from its key server (http://TV:9991/webos_rsa, encrypted with the passphrase) and keeps
    it in pc/lg-tv.json (private, not in git),
  - builds the .ipk package from tv-web/ itself (an ar archive with control.tar.gz and data.tar.gz, the layout webOS
    expects), copies it over SSH (port 9922, user "prisoner") and installs it with the TV's dev install service,
  - starts the app with the PC's address as launch parameters, every time translation starts, and installs a newer
    tv-web/ by itself when the version differs,
  - renews the 1000-hour developer mode session once a day (the same request as EXTEND in the Developer Mode app):
    when the session runs out LG turns developer mode off and removes the app.
"""
from __future__ import annotations

import base64
import gzip
import io
import json
import logging
import math
import re
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import paths

APP_ID = "com.dilmac.tv"
SSH_PORT = 9922
KEY_PORT = 9991
SSAP_PORTS = (3000, 3001)   # LG's remote control service: answers on every LG webOS TV, developer mode or not
TEMP_DIR = "/media/developer/temp"
APPS_DIR = "/media/developer/apps/usr/palm/applications"
SESSION_TOKEN_FILE = "/var/luna/preferences/devmode_enabled"
RESET_SESSION_URL = "https://developer.lge.com/secure/ResetDevModeSession.dev?sessionToken="
EXTEND_EVERY = 20 * 3600    # renew the developer mode session at most this often (s)
# Files of tv-web/ that are not for webOS (the Samsung package's own files).
_NOT_FOR_WEBOS = re.compile(r"^(platform-.*|config\.xml)$")

# paramiko logs through the logging module; in the windowed exe there is no stderr to print warnings to.
logging.getLogger("paramiko").addHandler(logging.NullHandler())
logging.getLogger("paramiko").propagate = False


class LgError(Exception):
    """A step failed; the message (Turkish) is shown to the user."""


# ---------------------------------------------------------------- the .ipk package

def web_dir() -> Path:
    d = paths.tv_web_dir()
    if d is None:
        raise LgError("TV uygulamasının dosyaları (tv-web klasörü) bulunamadı.")
    return d


def app_info(src: Path | None = None) -> dict:
    return json.loads(((src or web_dir()) / "appinfo.json").read_text(encoding="utf-8"))


def web_version() -> str:
    return str(app_info().get("version", ""))


def _icon_png(size: int) -> bytes:
    """The app icon: dark blue, two subtitle bars and "TR" (like the PC app's icon)."""
    from PIL import Image, ImageDraw, ImageFont

    k = size / 256
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=round(52 * k), fill=(0, 55, 145))
    d.rounded_rectangle([44 * k, 150 * k, 212 * k, 172 * k], radius=round(11 * k), fill=(255, 255, 255))
    d.rounded_rectangle([72 * k, 190 * k, 184 * k, 210 * k], radius=round(10 * k), fill=(170, 200, 255))
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\segoeuib.ttf", round(92 * k))
    except OSError:
        font = ImageFont.load_default()
    d.text((size / 2, 82 * k), "TR", font=font, fill=(255, 255, 255), anchor="mm")
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def _targz(entries: list[tuple[str, bytes | None]], mtime: int) -> bytes:
    """A .tar.gz; entries are (path, content) with content None for a folder. Owner root, as dpkg/opkg expect."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name, content in entries:
            info = tarfile.TarInfo("./" + name if name else ".")
            info.mtime = mtime
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            if content is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            else:
                info.mode = 0o644
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return gzip.compress(raw.getvalue(), mtime=mtime)


def _ar(members: list[tuple[str, bytes]], mtime: int) -> bytes:
    """A plain (Debian style) ar archive."""
    out = bytearray(b"!<arch>\n")
    for name, data in members:
        header = f"{name:<16}{mtime:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode("ascii")
        assert len(header) == 60
        out += header + data
        if len(data) % 2:
            out += b"\n"
    return bytes(out)


def build_ipk(src: Path | None = None, mtime: int | None = None) -> bytes:
    """The webOS package of tv-web/: usr/palm/applications/<id>/... and usr/palm/packages/<id>/packageinfo.json."""
    src = src or web_dir()
    info = app_info(src)
    app_id, version = str(info["id"]), str(info["version"])
    mtime = int(time.time()) if mtime is None else mtime
    files: dict[str, bytes] = {}
    for p in sorted(src.iterdir()):
        if p.is_file() and p.suffix.lower() in (".html", ".js", ".css", ".json", ".png") and not _NOT_FOR_WEBOS.match(p.name):
            files[p.name] = p.read_bytes()
    files.setdefault(str(info.get("icon", "icon.png")), _icon_png(80))
    files.setdefault(str(info.get("largeIcon", "largeIcon.png")), _icon_png(130))
    package_info = {"app": app_id, "id": app_id, "loc_name": info.get("title", app_id), "package_format_version": 2,
                    "vendor": info.get("vendor", ""), "version": version}
    app_dir = f"usr/palm/applications/{app_id}/"
    pkg_dir = f"usr/palm/packages/{app_id}/"
    entries: list[tuple[str, bytes | None]] = [
        ("", None), ("usr/", None), ("usr/palm/", None), ("usr/palm/applications/", None), (app_dir, None),
        ("usr/palm/packages/", None), (pkg_dir, None),
    ]
    entries += [(app_dir + name, data) for name, data in files.items()]
    entries.append((pkg_dir + "packageinfo.json", json.dumps(package_info, ensure_ascii=False, indent=2).encode("utf-8")))
    size_kb = math.ceil(sum(len(d) for _, d in entries if d) / 1024)
    control = (f"Package: {app_id}\nVersion: {version}\nSection: misc\nPriority: optional\nArchitecture: all\n"
               f"Installed-Size: {size_kb}\nMaintainer: Dilmac <nobody@localhost>\n"
               f"Description: {info.get('title', app_id)}\nwebOS-Package-Format-Version: 2\n"
               f"webOS-Packager-Version: 1.0.0\n")
    return _ar([("debian-binary", b"2.0\n"),
                ("control.tar.gz", _targz([("", None), ("control", control.encode("utf-8"))], mtime)),
                ("data.tar.gz", _targz(entries, mtime))], mtime)


# ---------------------------------------------------------------- the TV's key

def load_state() -> dict:
    """Encrypted for this Windows user (secure_store, DPAPI): the TV's SSH key must not be readable in a synced copy."""
    import secure_store

    try:
        data = secure_store.read_json(paths.LG_FILE)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, secure_store.SecretError):
        return {}


def save_state(state: dict) -> None:
    import secure_store

    secure_store.write_json(paths.LG_FILE, state)


def has_key() -> bool:
    return bool(load_state().get("key"))


def private_key(pem: str, passphrase: str | None):
    """The paramiko key from the PEM text (RSA as LG gives it; ECDSA / Ed25519 just in case)."""
    import paramiko

    last: Exception | None = None
    for cls in (paramiko.RSAKey, paramiko.ECDSAKey, paramiko.Ed25519Key):
        try:
            return cls.from_private_key(io.StringIO(pem), password=passphrase or None)
        except paramiko.PasswordRequiredException:
            raise LgError("TV'nin anahtarı şifreli; Developer Mode uygulamasındaki Passphrase'i gir.")
        except (paramiko.SSHException, ValueError, TypeError) as e:
            last = e
    raise LgError(f"Anahtar açılamadı; Passphrase yanlış olabilir (büyük/küçük harf önemli). ({last})")


def fetch_key(ip: str, passphrase: str) -> None:
    """Downloads the SSH key from the TV's key server, checks that the passphrase opens it and saves both."""
    passphrase = passphrase.strip()
    if not passphrase:
        raise LgError("Passphrase boş. TV'deki Developer Mode uygulamasının sol altında yazan 6 karakteri gir.")
    try:
        with urllib.request.urlopen(f"http://{ip}:{KEY_PORT}/webos_rsa", timeout=6) as r:
            pem = r.read(64_000).decode("ascii", "replace")
    except (urllib.error.URLError, OSError) as e:
        raise LgError("TV'nin anahtar sunucusuna ulaşılamadı. TV'de Developer Mode uygulamasını aç, 'Key Server'ı "
                      f"aç ve uygulama ekranda kalsın. ({e})")
    pem = pem.replace("\r\n", "\n").strip() + "\n"
    if "PRIVATE KEY" not in pem:
        raise LgError("TV'nin anahtar sunucusu beklenmeyen bir yanıt verdi.")
    private_key(pem, passphrase)
    state = load_state()
    state.update({"ip": ip, "key": pem, "passphrase": passphrase, "saved_at": int(time.time())})
    state.pop("host_key", None)  # a new setup (developer mode may have been reinstalled): the TV is remembered again
    save_state(state)


# ---------------------------------------------------------------- SSH

def connect(ip: str, timeout: float = 8):
    """An SSH connection to the TV's developer mode (port 9922, user prisoner)."""
    import paramiko

    state = load_state()
    if not state.get("key"):
        raise LgError("LG TV kurulmadı. TV satırında 'Kur'a bas.")
    key = private_key(state["key"], state.get("passphrase"))
    # The TV's own SSH host key is remembered at the first connection after setup and required from then on (for any
    # address the TV gets): a device pretending to be the TV on the network is refused before we log in.
    pinned = str(state.get("host_key") or "")
    # Some TVs' SSH servers only know the old "ssh-rsa" signature; try the modern ones first.
    for disabled in (None, {"pubkeys": ["rsa-sha2-512", "rsa-sha2-256"]}):
        client = paramiko.SSHClient()
        if pinned:
            kind, _, b64 = pinned.partition(" ")
            try:
                host_key = paramiko.PKey.from_type_string(kind, base64.b64decode(b64))
            except Exception:
                raise LgError("Kayıtlı TV kimliği okunamadı; TV satırında 'Kur'a basıp kurulumu yenile.")
            client.get_host_keys().add(f"[{ip}]:{SSH_PORT}", kind, host_key)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(ip, port=SSH_PORT, username="prisoner", pkey=key, timeout=timeout, banner_timeout=timeout,
                           auth_timeout=timeout, look_for_keys=False, allow_agent=False, disabled_algorithms=disabled)
            if not pinned:
                server_key = client.get_transport().get_remote_server_key()
                state["host_key"] = f"{server_key.get_name()} {server_key.get_base64()}"
                save_state(state)
            return client
        except paramiko.BadHostKeyException:
            client.close()
            raise LgError(f"{ip} adresindeki cihazın kimliği kayıtlı LG TV'ninkiyle aynı değil; bağlanılmadı. Ağda TV'yi "
                          "taklit eden bir cihaz olabilir. TV'yi sıfırladıysan TV satırında 'Kur' ile kurulumu yenile.")
        except paramiko.AuthenticationException:
            client.close()
        except (OSError, EOFError, paramiko.SSHException) as e:
            client.close()
            raise LgError(f"TV'ye bağlanılamadı ({ip}:{SSH_PORT}): {e}. TV açık ve geliştirici modu açık mı? Süresi "
                          "dolduysa Developer Mode uygulamasında yeniden aç.")
    raise LgError("TV kayıtlı anahtarı kabul etmedi (geliştirici modu yeniden kurulmuş olabilir). TV satırında 'Kur'a "
                  "basıp Passphrase'i yeniden gir.")


def run(client, cmd: str, timeout: float = 20, stdin: bytes | None = None) -> tuple[int, str]:
    """Runs a command on the TV; (exit code, stdout + stderr). SSH errors become LgError."""
    import paramiko

    try:
        chan = client.get_transport().open_session()
    except (paramiko.SSHException, EOFError, OSError) as e:
        raise LgError(f"TV komutu çalıştırılamadı ({cmd.split(' ')[0]}): {e}")
    try:
        chan.settimeout(timeout)
        try:
            chan.exec_command(cmd)
        except (paramiko.SSHException, EOFError, OSError) as e:
            raise LgError(f"TV komutu çalıştırılamadı ({cmd.split(' ')[0]}): {e}")
        if stdin is not None:
            chan.sendall(stdin)
            chan.shutdown_write()
        out = bytearray()
        end = time.monotonic() + timeout
        while True:
            if chan.recv_ready():
                out += chan.recv(65536)
            elif chan.recv_stderr_ready():
                out += chan.recv_stderr(65536)
            elif chan.exit_status_ready():
                break
            elif time.monotonic() > end:
                raise LgError(f"TV komutu zaman aşımına uğradı: {cmd.split(' ')[0]}")
            else:
                time.sleep(0.03)
        return chan.recv_exit_status(), out.decode("utf-8", "replace")
    finally:
        chan.close()


def _luna_command(uri: str, params: dict, interactive: bool = False) -> str:
    payload = json.dumps(params, ensure_ascii=True, separators=(",", ":"))
    if "'" in payload:
        raise LgError("Geçersiz karakter")
    return f"luna-send-pub {'-i' if interactive else '-n 1'} '{uri}' '{payload}'"


def luna(client, uri: str, params: dict, timeout: float = 20) -> dict:
    """A one-answer call to a webOS system service."""
    code, out = run(client, _luna_command(uri, params), timeout)
    try:
        return json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    except ValueError:
        return {"returnValue": False, "raw": out.strip()[:300], "exit": code}


def upload(client, data: bytes, path: str) -> None:
    try:
        sftp = client.open_sftp()
        try:
            with sftp.open(path, "wb") as f:
                f.set_pipelined(True)
                f.write(data)
        finally:
            sftp.close()
    except Exception:
        # No SFTP on the TV's SSH server: stream the file through cat.
        code, out = run(client, f"cat > {path}", 60, stdin=data)
        if code != 0:
            raise LgError(f"Paket TV'ye kopyalanamadı: {out[:200]}")
    code, out = run(client, f"wc -c < {path}")
    if code != 0 or out.strip() != str(len(data)):
        raise LgError(f"Paket TV'ye eksik kopyalandı ({out.strip()} / {len(data)} bayt).")


def installed_version(client) -> str | None:
    code, out = run(client, f"cat {APPS_DIR}/{APP_ID}/appinfo.json")
    if code != 0:
        return None
    try:
        return str(json.loads(out).get("version") or "") or None
    except ValueError:
        return None


def install(client, log) -> None:
    data = build_ipk()
    path = f"{TEMP_DIR}/dilmac.ipk"
    run(client, f"mkdir -p {TEMP_DIR}")
    log(f"Paket TV'ye gönderiliyor ({len(data) // 1024} KB)…")
    upload(client, data, path)
    log("TV'de kuruluyor…")
    cmd = _luna_command("luna://com.webos.appInstallService/dev/install",
                        {"id": APP_ID, "ipkUrl": path, "subscribe": True}, interactive=True)
    chan = client.get_transport().open_session()
    outcome, text = None, ""
    try:
        chan.exec_command(cmd)
        end = time.monotonic() + 180
        buf = ""
        while outcome is None and time.monotonic() < end:
            if chan.recv_ready():
                buf += chan.recv(65536).decode("utf-8", "replace")
            elif chan.recv_stderr_ready():
                buf += chan.recv_stderr(65536).decode("utf-8", "replace")
            elif chan.exit_status_ready():
                outcome = outcome or "ended"
            else:
                time.sleep(0.05)
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                text = line
                state = install_state(line)
                if state:
                    outcome = state
                    break
        if outcome in (None, "ended") and buf.strip():
            text = buf.strip()
            outcome = install_state(text) or outcome
    finally:
        chan.close()
        try:
            run(client, f"rm -f {path}")
        except LgError:
            pass
    if outcome == "installed":
        return
    if outcome == "failed":
        raise LgError(f"TV kurulumu reddetti: {text[:300]}")
    # No clear answer (or the call ended): look at what is on the TV.
    if installed_version(client) == app_info().get("version"):
        return
    raise LgError(f"Kurulum bitmedi ({outcome or 'zaman aşımı'}): {text[:300]}")


def install_state(line: str) -> str | None:
    """One answer line of the dev install service: "installed", "failed" or None (still going / unknown)."""
    try:
        r = json.loads(line)
    except ValueError:
        low = line.lower()
        return "failed" if ("not found" in low or "denied" in low or "error" in low) else None
    if not isinstance(r, dict):
        return None
    details = r.get("details") if isinstance(r.get("details"), dict) else {}
    state = str(details.get("state") or r.get("state") or "").lower()
    if state == "installed":
        return "installed"
    if "fail" in state or r.get("returnValue") is False or details.get("errorCode") not in (None, 0, "0"):
        return "failed"
    return None


def launch(client, params: dict) -> None:
    """Starts the app (or brings it to the front with new parameters if it is running)."""
    r = luna(client, "luna://com.webos.applicationManager/launch", {"id": APP_ID, "params": params})
    if r.get("returnValue") is False:
        raise LgError(f"Uygulama açılamadı: {json.dumps(r, ensure_ascii=False)[:300]}")


def extend_session(client, log, force: bool = False) -> None:
    """Renews the 1000-hour developer mode session (what EXTEND in the Developer Mode app does)."""
    state = load_state()
    if not force and time.time() - float(state.get("extended_at") or 0) < EXTEND_EVERY:
        return
    code, token = run(client, f"cat {SESSION_TOKEN_FILE}")
    token = token.strip()
    if code != 0 or not re.fullmatch(r"[A-Za-z0-9_\-+=/.]{8,400}", token):
        log("LG geliştirici modu süresi uzatılamadı (TV oturum bilgisini vermedi). Developer Mode uygulamasında "
            "süreyi ara sıra EXTEND ile uzat.")
        return
    try:
        with urllib.request.urlopen(RESET_SESSION_URL + urllib.parse.quote(token, safe=""), timeout=10) as r:
            body = r.read(4000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        log(f"LG geliştirici modu süresi uzatılamadı ({e}).")
        return
    if "success" in body.lower():
        state["extended_at"] = int(time.time())
        save_state(state)
        log("LG geliştirici modu süresi yenilendi (1000 saat).")
    else:
        log(f"LG geliştirici modu süresi uzatılamadı: {body.strip()[:150]}")


# ---------------------------------------------------------------- whole steps (GUI / translator)

def open_app(ip: str, port: int, hdmi: int, log, force_extend: bool = False, key: str | None = None) -> bool:
    """At translation start: installs/updates the app if needed, renews the session, opens it with this PC's address."""
    import tv_web

    host = tv_web.local_address_for(ip)
    if not host:
        log(f"Bu bilgisayarın {ip} ağındaki adresi bulunamadı.")
        return False
    try:
        client = connect(ip)
        try:
            have, want = installed_version(client), web_version()
            if have != want:
                log(f"LG TV uygulaması {'güncelleniyor' if have else 'kuruluyor'} ({have or 'yok'} → {want})…")
                install(client, log)
                log("TV uygulaması kuruldu.")
            extend_session(client, log, force_extend)
            # key: the access key of this PC's TV page server (tv_web); only the TV app learns it, from here.
            launch(client, {"host": host, "port": port, "hdmi": hdmi, "key": key or tv_web.tv_token()})
        finally:
            client.close()
    except LgError as e:
        log(f"LG TV: {e}")
        return False
    log(f"LG TV'de Dilmaç açıldı; TV bu bilgisayara bağlanacak ({host}:{port}).")
    return True


def setup(ip: str, passphrase: str, port: int, hdmi: int, log) -> bool:
    """The GUI's install button: key from the TV, then install and open."""
    try:
        log("TV'nin anahtarı alınıyor…")
        fetch_key(ip, passphrase)
        log("Anahtar alındı.")
    except LgError as e:
        log(str(e))
        return False
    return open_app(ip, port, hdmi, log, force_extend=True)
