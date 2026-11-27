"""Packs the GUI as a single-file Windows app: "Dilmac.exe" in the top folder.

Usage:  py pc\\build_exe.py [--setup]
Needs:  py -m pip install pyinstaller sv-ttk  (and requirements.txt); --setup also Inno Setup 6
        (winget install JRSoftware.InnoSetup --scope user) and makes Dilmac-Setup.exe from installer\\dilmac.iss.

The pack server's app secret is not in git: it is taken from this PC's config.json (pack_secret, DPAPI) into a
temporary pc\\_build_config.py for the build and deleted right after, so the exe can reach the server.

The build folder is on an ASCII path (%LOCALAPPDATA%\\dilmac-exe): OneDrive syncing and the 'ı' in the path
must not get in the way. The exe uses the pc\\ folder next to it as its data folder (config.json, cache, frames,
glossary).
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PC = Path(__file__).resolve().parent
PROJECT = PC.parent
WORK_DIR = Path(os.environ.get("LOCALAPPDATA", PC)) / "dilmac-exe"
APP_NAME = "Dilmac"


def make_icon(dest: Path) -> None:
    """App icon: dark blue background, two subtitle lines and 'TR'."""
    dims = 256
    im = Image.new("RGBA", (dims, dims), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([8, 8, dims - 8, dims - 8], radius=52, fill=(0, 55, 145))
    d.rounded_rectangle([44, 150, 212, 172], radius=11, fill=(255, 255, 255))
    d.rounded_rectangle([72, 190, 184, 210], radius=10, fill=(170, 200, 255))
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\segoeuib.ttf", 92)
    except OSError:
        font = ImageFont.load_default()
    d.text((dims / 2, 82), "TR", font=font, fill=(255, 255, 255), anchor="mm")
    im.save(dest, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def find_platform_tools() -> Path | None:
    """Android SDK platform-tools with adb.exe and its two DLLs."""
    roots = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
             str(Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk")]
    for root in roots:
        if root:
            d = Path(root) / "platform-tools"
            if all((d / n).is_file() for n in ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll")):
                return d
    return None


BUILD_CONFIG = PC / "_build_config.py"
VERSION = time.strftime("%Y.%m.%d")


def write_build_config() -> bool:
    """The app secret for the exe (from this PC's encrypted config); never printed, deleted after the build."""
    sys.path.insert(0, str(PC))
    import subtitler as A

    secret = str(A.load_config().get("pack_secret") or "")
    if not secret:
        print("UYARI: config.json'da pack_secret yok; exe paket sunucusuna bağlanamaz.")
        return False
    BUILD_CONFIG.write_text(f"APP_SECRET = {secret!r}\nVERSION = {VERSION!r}\n", encoding="utf-8")
    return True


def hashed_packs() -> Path | None:
    """The packs go into the exe only as hashed copies (pack.to_hashed): no game's English text in what we hand out.
    The app finds the lines by hash; fresh ones come from the pack server anyway."""
    import json

    sys.path.insert(0, str(PC))
    from pack import to_hashed

    src = PC / "packs"
    files = sorted(src.glob("*.json")) if src.is_dir() else []
    if not files:
        return None
    out = WORK_DIR / "packs-hashed"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        (out / f.name).write_text(json.dumps(to_hashed(data), ensure_ascii=False), encoding="utf-8")
    return out


def find_iscc() -> Path | None:
    for root in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles(x86)", ""),
                 os.environ.get("ProgramFiles", "")):
        p = Path(root) / ("Programs" if root == os.environ.get("LOCALAPPDATA") else "") / "Inno Setup 6" / "ISCC.exe"
        if root and p.is_file():
            return p
    return None


def build_setup() -> Path:
    iscc = find_iscc()
    if iscc is None:
        raise SystemExit("Inno Setup 6 yok: winget install JRSoftware.InnoSetup --scope user")
    import subprocess

    script = PROJECT / "installer" / "dilmac.iss"
    model = PC / "voices" / "model" / "nemo_en_titanet_small.onnx"
    args = [str(iscc), f"/DAppVersion={VERSION}", f"/DProject={PROJECT}"]
    if model.is_file():
        args.append(f"/DVoiceModel={model}")
    else:
        print("UYARI: ses modeli yok; kurulumda konuşanı sesinden tanıma kapalı olur.")
    subprocess.run(args + [str(script)], check=True)
    out = PROJECT / f"Dilmac-Setup-{VERSION}.exe"
    print(f"\nKurulum hazır: {out}  ({out.stat().st_size / 1e6:.1f} MB)")
    return out


def main() -> None:
    try:
        build(write_build_config())
    finally:
        BUILD_CONFIG.unlink(missing_ok=True)
    if "--setup" in sys.argv:
        build_setup()


def build(with_secret: bool) -> None:
    import PyInstaller.__main__

    icon = PC / "icon.ico"
    make_icon(icon)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    sep = os.pathsep
    data_ = [(PC / "glossary", "glossary"), (icon, ".")]
    hashed = hashed_packs()
    if hashed is not None:
        data_.append((hashed, "packs"))
    apk = PROJECT / "dilmac-tv.apk"
    if apk.is_file():
        data_.append((apk, "."))
    # The LG (and later Samsung) TV web app: lg_tv.build_ipk packs it on the user's PC.
    data_.append((PROJECT / "tv-web", "tv-web"))
    # Samsung's public intermediate certificates: the signature's certificate chain (samsung_tv.chain).
    data_.append((PC / "samsung_ca", "samsung_ca"))
    # The HTML GUI (gui_web.py) and pywebview with its Edge WebView2 loader (pythonnet).
    data_.append((PC / "ui", "ui"))
    args = [
        str(PC / "gui.py"),
        "--name", APP_NAME,
        "--onefile", "--windowed", "--noconfirm", "--clean",
        "--icon", str(icon),
        "--paths", str(PC),
        "--workpath", str(WORK_DIR / "build"),
        "--specpath", str(WORK_DIR),
        "--distpath", str(WORK_DIR / "dist"),
        "--collect-data", "spellchecker",
        "--collect-data", "sv_ttk",
        "--collect-all", "winrt",
        # Direct PS5 connection (ps5_direct.py): PyAV's FFmpeg decodes the video.
        "--collect-all", "av",
        # Speaker recognition by voice (audio.py): sherpa-onnx + onnxruntime DLLs. The model is not in the exe, it is
        # in pc\voices\model.
        "--collect-all", "sherpa_onnx",
        "--collect-all", "webview",
        # OPUS-MT translation (opus_mt.py); the model itself is downloaded into pc\models\ on first use.
        "--collect-all", "ctranslate2",
        "--collect-all", "sentencepiece",
        "--collect-all", "clr_loader",
        "--collect-all", "pythonnet",
        "--exclude-module", "pyremoteplay",
        "--exclude-module", "PySide6",
    ]
    if with_secret:
        args += ["--hidden-import", "_build_config"]
    for source, dest in data_:
        args += ["--add-data", f"{source}{sep}{dest}"]
    # chiaki-ng's connection library and the bridge (built with chiaki_bridge/build.sh) go into the "chiaki" folder
    # in the exe.
    bridge = PC / "chiaki_bridge" / "bin"
    if (bridge / "chiaki_bridge.dll").is_file():
        for dll in sorted(bridge.glob("*.dll")):
            args += ["--add-binary", f"{dll}{sep}chiaki"]
    else:
        print("UYARI: chiaki_bridge/bin/chiaki_bridge.dll yok; doğrudan PS5 bağlantısı exe'de çalışmaz.")
    # adb (Android platform-tools) goes into the "adb" folder: the TV row's install button must work on PCs without
    # the Android SDK (checks.adb_path looks there first).
    tools = find_platform_tools()
    if tools is not None:
        for name in ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"):
            args += ["--add-binary", f"{tools / name}{sep}adb"]
    else:
        print("UYARI: adb (Android SDK platform-tools) bulunamadı; TV uygulamasını kurma düğmesi exe'de çalışmaz.")
    PyInstaller.__main__.run(args)
    output = WORK_DIR / "dist" / f"{APP_NAME}.exe"
    dest = PROJECT / f"{APP_NAME}.exe"
    shutil.copy2(output, dest)
    print(f"\nHazır: {dest}  ({dest.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    sys.exit(main())
