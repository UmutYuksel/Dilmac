"""Dilmaç - desktop app entry point.

The HTML GUI (gui_web.py, pc/ui/) in an Edge WebView2 window; the older Tkinter GUI (gui_tk.py) when WebView2 or
pywebview is missing. --start (older name --baslat) starts translating as soon as the window is up (used after an
update while playing). --tk opens the Tkinter GUI.
"""
from __future__ import annotations

import sys


def selftest() -> None:
    """--selftest: loads the parts that come from compiled libraries and writes the result to the log (pc\\log.txt);
    for checking an exe on another computer."""
    import subtitler as A

    checks = {
        "webview": lambda: __import__("webview") and "ok",
        "paramiko (LG)": lambda: __import__("paramiko").__version__,
        "cryptography (Samsung)": lambda: __import__("cryptography").__version__,
        "ctranslate2": lambda: __import__("ctranslate2").__version__,
        "OCR": lambda: __import__("checks").check_ocr().txt,
    }

    def opus():
        import opus_mt

        if not opus_mt.installed():
            return "model indirilmemiş"
        m = opus_mt.model()
        return f"{m.device}: {m.translate(['We need to get out of here.'])[0]}"

    checks["OPUS-MT"] = opus
    for name, fn in checks.items():
        try:
            A.log(f"selftest {name}: {fn()}")
        except Exception as e:
            A.log(f"selftest {name}: HATA {type(e).__name__}: {e}")


def main() -> None:
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
        return
    if "--keygen" in args:
        # This PC's pack signing key (made once, encrypted for this Windows user); the public line goes to the log.
        import pack_server
        import subtitler as A

        kid, pub = pack_server.public_key_text(pack_server.signing_key(create=True))
        A.log(f'Paket imza anahtarı hazır. pack_server.PUBLISHER_KEYS içine ekle:  "{kid}": "{pub}",')
        return
    if "--nightly" in args or "--install-nightly" in args:
        import nightly

        nightly.main(args)
        return
    start = "--start" in args or "--baslat" in args
    if "--tk" not in args:
        try:
            import gui_web

            if gui_web.available():
                gui_web.main(start=start)
                return
        except Exception:
            import traceback

            import subtitler as A

            A.log("HTML arayüz açılamadı, eski arayüze geçiliyor:\n" + traceback.format_exc())
    import gui_tk

    gui_tk.main()


if __name__ == "__main__":
    main()
