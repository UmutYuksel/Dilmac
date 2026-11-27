"""Secrets at rest, encrypted with Windows DPAPI for the current Windows user.

The data folder (pc\\) can sit in a synced folder (OneDrive Desktop) and gets copied around; the PSN/PS5 pairing keys,
the LG TV's SSH key, the Samsung signing key, the Gemini key, the pool admin token and the pack signing key must not be
readable there. DPAPI ties them to this Windows account on this PC: a copied file is useless elsewhere.

Formats:
  values in config.json   "dpapi:<base64>"
  whole files             first line "PS5ALTYAZI-DPAPI1", then base64
Plain (older) values and files are still read, and written back encrypted the next time they are saved.

On a PC where DPAPI cannot open a blob (another user, a reinstalled Windows) the secret counts as missing: the app asks
for the pairing / install again instead of failing.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path

PREFIX = "dpapi:"
# Both still carry the app's old name (PS5 Altyazı): changing them would make every saved secret unreadable.
FILE_MAGIC = b"PS5ALTYAZI-DPAPI1\n"
ENTROPY = b"PS5 Altyazi secret store v1"


class SecretError(Exception):
    """The blob cannot be opened on this PC / for this user."""


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _call(fn, data: bytes) -> bytes:
    if os.name != "nt":  # tests on other systems: no encryption available
        raise SecretError("DPAPI yalnız Windows'ta var")
    src, _keep1 = _blob(data)
    ent, _keep2 = _blob(ENTROPY)
    out = _Blob()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise SecretError(f"DPAPI hatası {ctypes.GetLastError()}")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(data: bytes) -> bytes:
    c = ctypes.windll.crypt32
    return _call(lambda *a: c.CryptProtectData(a[0], None, *a[2:]), data)


def unprotect(blob: bytes) -> bytes:
    c = ctypes.windll.crypt32
    return _call(lambda *a: c.CryptUnprotectData(a[0], None, *a[2:]), blob)


# ---------------------------------------------------------------- single values (config.json)

def is_protected(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def protect_text(value: str) -> str:
    if not value or is_protected(value):
        return value
    return PREFIX + base64.b64encode(protect(value.encode("utf-8"))).decode("ascii")


def unprotect_text(value) -> str:
    """The plain value; an older plain value as it is; "" if the blob is from another PC/user."""
    if not is_protected(value):
        return value or ""
    try:
        return unprotect(base64.b64decode(value[len(PREFIX):])).decode("utf-8")
    except (SecretError, ValueError, UnicodeDecodeError):
        return ""


# ---------------------------------------------------------------- whole files

def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(FILE_MAGIC + base64.b64encode(protect(data)) + b"\n")
    tmp.replace(path)


def read_bytes(path: Path) -> bytes:
    """The file's content; an older plain file as it is. SecretError if it cannot be opened here."""
    raw = path.read_bytes()
    if not raw.startswith(FILE_MAGIC):
        return raw
    try:
        return unprotect(base64.b64decode(raw[len(FILE_MAGIC):].strip()))
    except ValueError as e:
        raise SecretError(f"Bozuk şifreli dosya: {path.name}") from e


def is_protected_file(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(len(FILE_MAGIC)) == FILE_MAGIC
    except OSError:
        return False


def write_json(path: Path, data) -> None:
    write_bytes(path, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))


def read_json(path: Path, migrate: bool = True):
    """JSON from an encrypted (or older plain) file; a plain file is encrypted in place (migrate)."""
    raw = read_bytes(path)
    data = json.loads(raw.decode("utf-8"))
    if migrate and not is_protected_file(path):
        try:
            write_json(path, data)
        except (OSError, SecretError):
            pass
    return data
