"""Dilmaç - the older desktop GUI (Tkinter). Used when the HTML GUI (gui_web.py) cannot start (no WebView2).

On the left the connections (PS5, Remote Play, TV, translation engine, text recognition, network) with status lights
and fix buttons; on the right the Live (read / translated lines), Settings and Subtitle region (select by dragging)
tabs. The translator (subtitler.run) runs on its own thread; its events reach the GUI through a queue.
"""
from __future__ import annotations

import asyncio
import queue
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import subtitler as A
import checks as B
import ocr as O
import paths
from translation import Glossary

COLORS = {"ok": "#3fb950", "warn": "#d29922", "bad": "#f85149", "unknown": "#6e7681"}
DIM = "#9da5b4"
REMOTE_PLAY_PAGE = "https://remoteplay.dl.playstation.net/remoteplay/lang/tr/index.html"
DEFAULT_REGION = dict(A.DEFAULTS["region"])


class Runner:
    """Runs the translator on its own thread with its own asyncio loop."""

    def __init__(self, q: queue.Queue):
        self.q = q
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, cfg: dict) -> None:
        def dest():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop = loop
            self.task = loop.create_task(A.run(cfg, notify=lambda event, **data_: self.q.put((event, data_))))
            try:
                loop.run_until_complete(self.task)
            except asyncio.CancelledError:
                pass
            except A.ConfigError as e:
                self.q.put(("error", {"txt": str(e)}))
            except Exception as e:
                A.log("Beklenmeyen hata:\n" + traceback.format_exc())
                self.q.put(("error", {"txt": f"Beklenmeyen hata: {e}"}))
            finally:
                loop.close()
                self.loop = self.task = None
                self.q.put(("stopped", {}))

        self.thread = threading.Thread(target=dest, daemon=True, name="translator")
        self.thread.start()

    def stop(self) -> None:
        loop, task = self.loop, self.task
        if loop is not None and task is not None:
            loop.call_soon_threadsafe(task.cancel)


class Line(ttk.Frame):
    """A connection row: status light, title, description, buttons."""

    def __init__(self, parent, heading: str, background: str):
        super().__init__(parent, padding=(12, 9))
        self.dot = tk.Canvas(self, width=14, height=14, highlightthickness=0, bg=background)
        self.oval = self.dot.create_oval(2, 2, 13, 13, fill=COLORS["unknown"], outline="")
        self.dot.grid(row=0, column=0, rowspan=2, sticky="n", pady=(5, 0), padx=(0, 10))
        ttk.Label(self, text=heading, font=("Segoe UI Semibold", 11)).grid(row=0, column=1, sticky="w")
        self.detail = ttk.Label(self, text="Denetleniyor…", foreground=DIM, wraplength=250, justify="left")
        self.detail.grid(row=1, column=1, sticky="w")
        self.bbox = ttk.Frame(self)
        self.bbox.grid(row=0, column=2, rowspan=2, sticky="e", padx=(8, 0))
        self.columnconfigure(1, weight=1)
        self.buttons: dict[str, ttk.Button] = {}

    def adjust(self, status: B.Status) -> None:
        self.dot.itemconfig(self.oval, fill=COLORS.get(status.level, COLORS["unknown"]))
        self.detail.config(text=status.txt)

    def button(self, name: str, txt: str, cmd, accent: bool = False) -> ttk.Button:
        b = ttk.Button(self.bbox, text=txt, command=cmd, style="Accent.TButton" if accent else "TButton")
        self.buttons[name] = b
        b.pack(side="left", padx=(4, 0))
        return b

    def show_btn(self, *names: str) -> None:
        """Show only the given buttons (their order is kept)."""
        for name, b in self.buttons.items():
            b.pack_forget()
        for name, b in self.buttons.items():
            if name in names:
                b.pack(side="left", padx=(4, 0))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Dilmaç")
        self.geometry("1180x760")
        self.minsize(1000, 640)
        icon = paths.BUNDLE / "icon.ico"
        if icon.is_file():
            try:
                self.iconbitmap(default=str(icon))
            except tk.TclError:
                pass
        try:
            import sv_ttk

            sv_ttk.set_theme("dark")
        except Exception:
            pass
        self.style_ = ttk.Style(self)
        self.background = self.style_.lookup("TFrame", "background") or "#1c1c1c"

        self.q: queue.Queue = queue.Queue()
        A.log_hook(lambda line_count: self.q.put(("log", {"line_count": line_count})))
        self.runner = Runner(self.q)
        self.cfg = A.load_config()
        self.statuses: dict[str, B.Status] = {}
        self._closing = False
        self._check_now_event = threading.Event()
        self._ps5_last = 0.0
        self._slow_last = 0.0

        self._top_bar()
        body = ttk.Frame(self, padding=(16, 0, 16, 16))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, minsize=410)  # the panel width must not jump when buttons appear and disappear
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        self._left_panel(body)
        self._tabs(body)

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._queue)
        threading.Thread(target=self._checker, daemon=True, name="from_checker").start()

    # ------------------------------------------------------------ layout

    def _top_bar(self) -> None:
        bar = ttk.Frame(self, padding=(20, 16, 20, 12))
        bar.pack(fill="x")
        left = ttk.Frame(bar)
        left.pack(side="left")
        ttk.Label(left, text="Dilmaç", font=("Segoe UI Semibold", 20)).pack(anchor="w")
        ttk.Label(left, text="Oyunun İngilizce altyazılarını Türkçeye çevirip TV'de gösterir",
                  foreground=DIM).pack(anchor="w")
        right_ = ttk.Frame(bar)
        right_.pack(side="right")
        self.start_btn = ttk.Button(right_, text="▶  Çeviriyi başlat", style="Accent.TButton", command=self._start_stop,
                                     width=22)
        self.start_btn.pack(side="right", ipady=6)
        self.status_lbl = ttk.Label(right_, text="Hazır değil", foreground=DIM)
        self.status_lbl.pack(side="right", padx=14)

    def _left_panel(self, body) -> None:
        left = ttk.LabelFrame(body, text=" Bağlantılar ", padding=(4, 6))
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        self.lines_: dict[str, Line] = {}

        def line_count(name, heading):
            s = Line(left, heading, self.background)
            s.pack(fill="x")
            ttk.Separator(left).pack(fill="x", padx=10)
            self.lines_[name] = s
            return s

        s = line_count("ps5", "PS5")
        s.button("search", "Ara", lambda: self._check_now(ps5=True))
        s.button("pair", "Eşle", self._pair_ps5)

        s = line_count("rp", "PS Remote Play")
        s.button("open", "Aç", self._open_remote_play)
        s.button("download", "İndir", lambda: __import__("os").startfile(REMOTE_PLAY_PAGE))

        s = line_count("tv", "TV uygulaması")
        s.button("find", "Bul", self._find_tv)
        s.button("install", "Kur", self._install_tv)
        s.button("test", "Dene", self._test_tv)

        s = line_count("engine", "Çeviri motoru")
        s.button("start", "Ollama'yı başlat", self._start_ollama)
        s.button("download", "Modeli indir", self._download_ollama)

        s = line_count("ocr", "Yazı tanıma (OCR)")
        s.button("install", "Kur", self._install_ocr)

        s = line_count("network", "Ağ profili")
        s.button("correct", "Özel yap", self._fix_network)

        for s in self.lines_.values():
            s.show_btn()
        ttk.Button(left, text="Hepsini yeniden denetle", command=lambda: self._check_now(ps5=True, slow=True)).pack(
            fill="x", padx=12, pady=(10, 6))

    def _tabs(self, body) -> None:
        self.nb = ttk.Notebook(body)
        self.nb.grid(row=0, column=1, sticky="nsew")
        self._live_tab()
        self._settings_tab()
        self._region_tab()

    # ------------------------------------------------------------ Live

    def _live_tab(self) -> None:
        f = ttk.Frame(self.nb, padding=16)
        self.nb.add(f, text="  Canlı  ")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(3, weight=1)
        card = ttk.LabelFrame(f, text=" Şu an ", padding=14)
        card.grid(row=0, column=0, sticky="ew")
        card.columnconfigure(0, weight=1)
        self.en_lbl = ttk.Label(card, text="Okunan İngilizce satır burada görünecek", foreground=DIM,
                                font=("Segoe UI", 12), wraplength=640, justify="left")
        self.en_lbl.grid(row=0, column=0, sticky="w")
        self.tr_lbl = ttk.Label(card, text="Türkçesi burada", font=("Segoe UI Semibold", 18), wraplength=640,
                                justify="left")
        self.tr_lbl.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.tr_info = ttk.Label(card, text="", foreground=DIM)
        self.tr_info.grid(row=2, column=0, sticky="w", pady=(4, 0))

        top = ttk.Frame(f)
        top.grid(row=2, column=0, sticky="ew", pady=(14, 4))
        ttk.Label(top, text="Günlük", font=("Segoe UI Semibold", 11)).pack(side="left")
        ttk.Button(top, text="Temizle", command=lambda: self.log_txt.delete("1.0", "end")).pack(side="right")
        bbox = ttk.Frame(f)
        bbox.grid(row=3, column=0, sticky="nsew")
        bbox.columnconfigure(0, weight=1)
        bbox.rowconfigure(0, weight=1)
        self.log_txt = tk.Text(bbox, wrap="word", height=10, bg="#161616", fg="#e6e6e6", insertbackground="#e6e6e6",
                               relief="flat", font=("Consolas", 10), padx=10, pady=8, borderwidth=0)
        self.log_txt.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(bbox, command=self.log_txt.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log_txt.config(yscrollcommand=sb.set)
        self.log_txt.tag_config("en", foreground="#8ab4f8")
        self.log_txt.tag_config("tr", foreground="#7ee787")
        self.log_txt.tag_config("error", foreground="#ff7b72")
        self.log_txt.tag_config("rehber", foreground=DIM)
        self.log_txt.insert("end", (
            "Nasıl kullanılır\n"
            "  1. PS5'i ve TV'yi aç. Android TV'de 'Dilmaç' uygulamasını bir kez aç, sonra PS5'in HDMI girişine geç.\n"
            "     LG ve Samsung TV'de uygulama çeviri başlayınca kendiliğinden açılır, PS5 görüntüsünü kendi içinde gösterir.\n"
            "     (Uygulama kurulu değilse soldaki TV satırında 'Kur'a bas.)\n"
            "  2. Soldaki 'PS Remote Play' satırında 'Aç'a bas ve PS5'e bağlan. Pencere açık ve görünür kalsın.\n"
            "  3. İlk seferde 'Altyazı alanı' sekmesinden altyazının çıktığı yeri seç.\n"
            "  4. Sağ üstteki 'Çeviriyi başlat'a bas. Okunan ve çevrilen satırlar burada görünür.\n\n"), "rehber")

    def _log(self, line_count: str) -> None:
        label = "tr" if "[TR" in line_count or "[geri" in line_count else "en" if "[EN" in line_count else \
            "error" if ("error" in line_count.lower() or "başarısız" in line_count or "ulaşılamadı" in line_count) else ""
        is_below = self.log_txt.yview()[1] > 0.98
        self.log_txt.insert("end", line_count + "\n", label)
        if int(self.log_txt.index("end-1c").split(".")[0]) > 3000:
            self.log_txt.delete("1.0", "500.0")
        if is_below:
            self.log_txt.see("end")

    # ------------------------------------------------------------ Ayarlar

    def _settings_tab(self) -> None:
        outer = ttk.Frame(self.nb, padding=(16, 12))
        self.nb.add(outer, text="  Ayarlar  ")
        # The save bar is placed first (at the bottom): it stays visible even in a short window.
        bottom = ttk.Frame(outer)
        bottom.pack(side="bottom", fill="x", pady=(10, 0))
        ttk.Button(bottom, text="Kaydet", style="Accent.TButton", command=self._save_settings, width=14).pack(side="left")
        self.record_lbl = ttk.Label(bottom, text="", foreground=DIM)
        self.record_lbl.pack(side="left", padx=12)
        f = ttk.Frame(outer)
        f.pack(fill="both", expand=True, anchor="n")
        f.columnconfigure(1, weight=1)
        c = self.cfg
        self.v = {
            "game": tk.StringVar(value=c.get("game", "")),
            "engine": tk.StringVar(value="google" if c.get("google_only") else c.get("engine", "local")),
            "local_model": tk.StringVar(value=c.get("local_model", "")),
            "gemini_api_key": tk.StringVar(value=c.get("gemini_api_key", "")),
            "gemini_model": tk.StringVar(value=c.get("gemini_model", "")),
            "tv_ip": tk.StringVar(value=c.get("tv_ip", "")),
            "tv_type": tk.StringVar(value=c.get("tv_type", "android")),
            "tv_hdmi": tk.IntVar(value=int(c.get("tv_hdmi") or 0)),
            "ps5_ip": tk.StringVar(value=c.get("ps5_ip", "")),
            "connection": tk.StringVar(value=c.get("connection", "window")),
            "font_px": tk.IntVar(value=A.font_px(c)),
            "box_opacity": tk.IntVar(value=round(float(c.get("box_opacity", 0.97)) * 100)),
            "fade_ms": tk.IntVar(value=int(c.get("fade_ms", 180))),
            "old_line_seconds": tk.DoubleVar(value=float(c.get("old_line_seconds", 3.0))),
            "linger_seconds": tk.DoubleVar(value=float(c.get("linger_seconds", 2.5))),
            "show_objectives": tk.BooleanVar(value=bool(c.get("show_objectives", True))),
            "split_blocks": tk.BooleanVar(value=bool(c.get("split_blocks", True))),
            "speaker_colors": tk.BooleanVar(value=bool(c.get("speaker_colors", True))),
            "voice_recognition": tk.BooleanVar(value=bool(c.get("voice_recognition", True))),
            "save_frames": tk.BooleanVar(value=bool(c.get("save_frames", False))),
        }
        r = 0

        def heading(txt):
            nonlocal r
            ttk.Label(f, text=txt, font=("Segoe UI Semibold", 12)).grid(row=r, column=0, columnspan=3, sticky="w",
                                                                         pady=(10 if r else 0, 4))
            r += 1

        def row(label, w, hint=""):
            """Without a hint the field spans two columns (so long options are not cut off)."""
            nonlocal r
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", padx=(0, 16), pady=3)
            w.grid(row=r, column=1, columnspan=1 if hint else 2, sticky="ew" if hint else "w", pady=3)
            if hint:
                ttk.Label(f, text=hint, foreground=DIM).grid(row=r, column=2, sticky="w", padx=(12, 0))
            r += 1
            return w

        heading("Oyun ve çeviri")
        games = [s.game_title for s in Glossary.all_glossaries()]
        row("Oyun", ttk.Combobox(f, textvariable=self.v["game"], values=games), "Sözlüğü olan oyunlarda adlar korunur")
        engine_name = ttk.Frame(f)
        for val, txt in (("opus", "Bilgisayarda (ücretsiz)"), ("local", "Yerel yapay zekâ (Ollama)"),
                         ("gemini", "Gemini (argoda iyi)"), ("google", "Google Çeviri")):
            ttk.Radiobutton(engine_name, text=txt, value=val, variable=self.v["engine"]).pack(side="left", padx=(0, 18))
        row("Çeviri motoru", engine_name)
        row("Yerel model", ttk.Entry(f, textvariable=self.v["local_model"]), "ör. gemma4:e4b-it-qat")
        text_key = ttk.Frame(f)
        text_key.columnconfigure(0, weight=1)
        self.key_entry = ttk.Entry(text_key, textvariable=self.v["gemini_api_key"], show="•")
        self.key_entry.grid(row=0, column=0, sticky="ew")
        show_btn = tk.BooleanVar(value=False)
        ttk.Checkbutton(text_key, text="Göster", variable=show_btn,
                        command=lambda: self.key_entry.config(show="" if show_btn.get() else "•")).grid(
            row=0, column=1, padx=(8, 0))
        row("Gemini anahtarı", text_key, "aistudio.google.com/apikey")
        row("Gemini modeli", ttk.Entry(f, textvariable=self.v["gemini_model"]))

        heading("Cihazlar")
        kind = ttk.Frame(f)
        for val, txt in (("android", "Android TV / Google TV"), ("lg", "LG (webOS)"), ("samsung", "Samsung (Tizen)")):
            ttk.Radiobutton(kind, text=txt, value=val, variable=self.v["tv_type"]).pack(side="left", padx=(0, 18))
        row("TV türü", kind)
        tv = ttk.Frame(f)
        tv.columnconfigure(0, weight=1)
        ttk.Entry(tv, textvariable=self.v["tv_ip"]).grid(row=0, column=0, sticky="ew")
        ttk.Button(tv, text="Ağda bul", command=self._find_tv).grid(row=0, column=1, padx=(8, 0))
        row("TV IP adresi", tv, "TV uygulamasının ekranında yazar")
        row("HDMI girişi", ttk.Spinbox(f, from_=0, to=4, increment=1, textvariable=self.v["tv_hdmi"], width=8),
            "LG / Samsung: PS5'in takılı olduğu giriş; 0 = otomatik. TV'de ◀ ▶ ile de değişir")
        row("PS5 IP adresi", ttk.Entry(f, textvariable=self.v["ps5_ip"]), "PS5 bulununca kendiliğinden dolar")
        source = ttk.Frame(f)
        for val, txt in (("window", "Remote Play penceresi"), ("direct", "Doğrudan PS5 (Remote Play'siz)")):
            ttk.Radiobutton(source, text=txt, value=val, variable=self.v["connection"]).pack(side="left", padx=(0, 18))
        row("Görüntü kaynağı", source)

        heading("TV'deki yazı (İngilizcenin tam üstüne oturur)")
        px = ttk.Frame(f)
        ttk.Spinbox(px, from_=16, to=80, increment=2, textvariable=self.v["font_px"], width=8).pack(side="left")
        ttk.Button(px, text="TV'de dene", command=self._try_font_size).pack(side="left", padx=(8, 0))
        ttk.Label(px, text=f"Sabit kalır; {A.BASE_FONT_PX} = oyunun altyazısı kadar",
                  foreground=DIM).pack(side="left", padx=(12, 0))
        row("Yazı boyutu (punto)", px)
        row("Kutu opaklığı (%)", ttk.Spinbox(f, from_=50, to=100, increment=1, textvariable=self.v["box_opacity"],
                                             width=8), "100 = İngilizce hiç görünmez")
        row("Geçiş (ms)", ttk.Spinbox(f, from_=0, to=600, increment=20, textvariable=self.v["fade_ms"], width=8),
             "Kutu yumuşakça belirip söner; 0 = anında")
        row("Eski satır (sn)", ttk.Spinbox(f, from_=0, to=8, increment=0.5, textvariable=self.v["old_line_seconds"],
                                           width=8), "Ard arda altyazıda önceki üstte soluk kalır; 0 = kapalı")
        row("Kalma süresi (sn)", ttk.Spinbox(f, from_=1, to=10, increment=0.5,
                                             textvariable=self.v["linger_seconds"], width=8),
             "Orijinal altyazı gittikten sonra")
        row("Görev yazıları", ttk.Checkbutton(f, text="\"Go to the docks\" gibi görevleri ayrı kutuda çevir",
                                               variable=self.v["show_objectives"]))
        row("Üst üste altyazı", ttk.Checkbutton(f, text="NPC bağırışı gibi üst üste binen altyazıları ayrı ayrı "
                                                        "çevir, her biri kendi kutusunda",
                                                 variable=self.v["split_blocks"]))
        row("Konuşan rengi", ttk.Checkbutton(f, text="Paketteki satırlarda yazı konuşanın renginde: Lucia pembe, Jason "
                                                     "mavi, diğerleri sarı, yaya/polis gri",
                                              variable=self.v["speaker_colors"]))
        row("Sesten tanıma", ttk.Checkbutton(f, text="Doğrudan PS5'te oyunun sesini de al, replik başına kısa ses "
                                                     "parçası kaydet (konuşanı sesinden tanımak için). TV'nin sesi "
                                                     "kesilirse kapat.", variable=self.v["voice_recognition"]))

        heading("Deneme")
        row("Kare kaydı", ttk.Checkbutton(f, text="Çevrilen satırların görüntüsünü pc\\kareler klasörüne kaydet "
                                                   "(OCR ayarı için)", variable=self.v["save_frames"]))

    def _save_settings(self, quiet: bool = False) -> bool:
        try:
            user = A.user_config()
        except Exception as e:
            messagebox.showerror("Ayarlar", f"config.json okunamadı: {e}")
            return False
        v = self.v
        try:
            new = {
                "game": v["game"].get().strip(),
                "engine": "local" if v["engine"].get() == "google" else v["engine"].get(),
                "google_only": v["engine"].get() == "google",
                "local_model": v["local_model"].get().strip(),
                "gemini_api_key": v["gemini_api_key"].get().strip(),
                "gemini_model": v["gemini_model"].get().strip(),
                "tv_ip": v["tv_ip"].get().strip(),
                "tv_type": v["tv_type"].get() if v["tv_type"].get() in ("android", "lg", "samsung") else "android",
                "tv_hdmi": max(0, min(4, int(v["tv_hdmi"].get()))),
                "ps5_ip": v["ps5_ip"].get().strip(),
                "connection": v["connection"].get(),
                "font_px": max(16, min(80, int(v["font_px"].get()))),
                "box_opacity": max(0.3, min(1.0, int(v["box_opacity"].get()) / 100)),
                "fade_ms": max(0, min(600, int(v["fade_ms"].get()))),
                "old_line_seconds": max(0.0, float(v["old_line_seconds"].get())),
                "linger_seconds": float(v["linger_seconds"].get()),
                "show_objectives": bool(v["show_objectives"].get()),
                "split_blocks": bool(v["split_blocks"].get()),
                "speaker_colors": bool(v["speaker_colors"].get()),
                "voice_recognition": bool(v["voice_recognition"].get()),
                "save_frames": bool(v["save_frames"].get()),
            }
        except (tk.TclError, ValueError):
            messagebox.showerror("Ayarlar", "Sayı alanlarından biri geçersiz.")
            return False
        user.update(new)
        user.pop("font_scale", None)  # replaced by font_px
        A.save_config(user)
        self.cfg = A.load_config()
        self._check_now()
        if not quiet:
            extra = " Çeviri yeniden başlatılıyor…" if self.runner.running else ""
            self.record_lbl.config(text=f"Kaydedildi ({time.strftime('%H:%M:%S')}).{extra}")
            if self.runner.running:
                self._restart = True
                self.runner.stop()
        return True

    # ------------------------------------------------------------ Subtitle region

    def _region_tab(self) -> None:
        f = ttk.Frame(self.nb, padding=16)
        self.nb.add(f, text="  Altyazı alanı  ")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        ttk.Label(f, text="Remote Play'den bir görüntü al, sonra altyazının çıktığı yeri fareyle sürükleyerek seç. "
                          "Alan, iki satırlık altyazıyı rahatça kapsamalı.",
                  foreground=DIM, wraplength=700, justify="left").grid(row=0, column=0, sticky="w")
        self.canvas = tk.Canvas(f, bg="#101010", highlightthickness=0, height=400)
        self.canvas.grid(row=1, column=0, sticky="nsew", pady=10)
        self.canvas.bind("<ButtonPress-1>", self._drag_begin)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.canvas.bind("<Configure>", lambda e: self._draw_canvas())
        btns = ttk.Frame(f)
        btns.grid(row=2, column=0, sticky="ew")
        ttk.Button(btns, text="Remote Play'den görüntü al", style="Accent.TButton",
                   command=self._grab_frame).pack(side="left")
        ttk.Button(btns, text="Resim aç…", command=self._open_image).pack(side="left", padx=6)
        ttk.Button(btns, text="Bu alanı oku", command=self._read_region).pack(side="left", padx=6)
        ttk.Button(btns, text="Varsayılan alan", command=self._region_default).pack(side="left", padx=6)
        ttk.Button(btns, text="Alanı kaydet", command=self._save_region).pack(side="right")
        self.region_lbl = ttk.Label(f, text="", foreground=DIM, wraplength=800, justify="left")
        self.region_lbl.grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.frame: Image.Image | None = None
        self.frame_tk = None
        self.row = dict(self.cfg["region"])
        self._frame_box = (0, 0, 1, 1)
        self._drag_origin = None
        self._show_region()

    def _draw_canvas(self) -> None:
        t = self.canvas
        t.delete("all")
        cw, ch = max(1, t.winfo_width()), max(1, t.winfo_height())
        x0, y0, w, h = A.fit_16_9(cw, ch)
        self._frame_box = (x0, y0, w, h)
        if self.frame is not None:
            self.frame_tk = ImageTk.PhotoImage(self.frame.resize((w, h), Image.BILINEAR))
            t.create_image(x0, y0, anchor="nw", image=self.frame_tk)
        else:
            t.create_rectangle(x0, y0, x0 + w, y0 + h, outline="#30363d")
            t.create_text(x0 + w / 2, y0 + h / 2, fill=DIM, font=("Segoe UI", 12),
                          text="Görüntü yok. Remote Play açıkken 'Remote Play'den görüntü al'a bas.")
        a = self.row
        t.create_rectangle(x0 + a["left"] * w, y0 + a["top"] * h, x0 + (a["left"] + a["width"]) * w,
                           y0 + (a["top"] + a["height"]) * h, outline="#ff4d4d", width=3, tags="region")

    def _ratio(self, x, y):
        x0, y0, w, h = self._frame_box
        return min(1, max(0, (x - x0) / w)), min(1, max(0, (y - y0) / h))

    def _drag_begin(self, e):
        self._drag_origin = self._ratio(e.x, e.y)

    def _drag(self, e):
        if self._drag_origin is None:
            return
        (ax, ay), (bx, by) = self._drag_origin, self._ratio(e.x, e.y)
        if abs(bx - ax) < 0.02 or abs(by - ay) < 0.02:
            return
        self.row = {"left": round(min(ax, bx), 3), "top": round(min(ay, by), 3),
                     "width": round(abs(bx - ax), 3), "height": round(abs(by - ay), 3)}
        self._draw_canvas()
        self._show_region()

    def _drag_end(self, e):
        self._drag_origin = None

    def _show_region(self, extra: str = "") -> None:
        a = self.row
        txt = (f"Alan: soldan %{a['left'] * 100:.0f}, üstten %{a['top'] * 100:.0f}, "
                 f"genişlik %{a['width'] * 100:.0f}, yükseklik %{a['height'] * 100:.0f}")
        registered = self.cfg["region"]
        if any(abs(a[k] - registered[k]) > 1e-6 for k in a):
            txt += "   (kaydedilmedi)"
        self.region_lbl.config(text=txt + ("\n" + extra if extra else ""))

    def _grab_frame(self) -> None:
        matches = A.find_window(self.cfg["window_titles"])
        if not matches:
            messagebox.showinfo("Altyazı alanı", "Remote Play penceresi bulunamadı. Önce Remote Play'i açıp PS5'e bağlan.")
            return
        vr = A.video_rect(matches[0])
        if vr is None:
            messagebox.showinfo("Altyazı alanı", "Remote Play penceresi simge durumunda; pencereyi aç.")
            return
        x, y, w, h = vr
        try:
            self.frame = A.grab(matches[0], {"left": x, "top": y, "width": w, "height": h})
        except Exception as e:
            messagebox.showerror("Altyazı alanı", f"Görüntü alınamadı: {e}")
            return
        self._draw_canvas()
        self._show_region(f"Görüntü: {w}×{h}")

    def _open_image(self) -> None:
        path_ = filedialog.askopenfilename(title="Oyun karesi seç",
                                         filetypes=[("Resim", "*.png *.jpg *.jpeg *.bmp"), ("Hepsi", "*.*")])
        if not path_:
            return
        try:
            im = Image.open(path_).convert("RGB")
        except Exception as e:
            messagebox.showerror("Resim", f"Açılamadı: {e}")
            return
        x, y, w, h = A.fit_16_9(*im.size)
        self.frame = im.crop((x, y, x + w, y + h))
        self._draw_canvas()
        self._show_region(f"Görüntü: {w}×{h}")

    def _read_region(self) -> None:
        if self.frame is None:
            self._grab_frame()
            if self.frame is None:
                return
        frame, row, cfg = self.frame, dict(self.row), dict(self.cfg)
        self._show_region("Okunuyor…")

        def job():
            try:
                w, h = frame.size
                bbox = A.subtitle_box((0, 0, w, h), row)
                crop = frame.crop((bbox["left"], bbox["top"], bbox["left"] + bbox["width"],
                                  bbox["top"] + bbox["height"]))
                glossary, lexicon = A.make_parts(cfg)
                cfg["region"] = row
                reader = O.Reader(A.make_ocr_engine(), lexicon, float(cfg.get("ocr_scale") or 0),
                                    A.make_layout(cfg, glossary))
                r = asyncio.run(reader.read(crop, h))
                if r.turkish:
                    outcome = f"Türkçe yazı (çevrilmez): {r.raw_}"
                elif r.txt:
                    outcome = f"Okunan [{r.prep}]: {r.txt}"
                else:
                    outcome = "Altyazı okunmadı" + (f" (ham: {r.raw_})" if r.raw_ else "")
                if r.objective:
                    outcome += f"\nGörev: {r.objective}"
            except Exception as e:
                outcome = f"Okuma hatası: {e}"
            self.q.put(("alan_sonuc", {"txt": outcome}))

        threading.Thread(target=job, daemon=True).start()

    def _region_default(self) -> None:
        self.row = dict(DEFAULT_REGION)
        self._draw_canvas()
        self._show_region()

    def _save_region(self) -> None:
        user = A.user_config()
        user["region"] = dict(self.row)
        A.save_config(user)
        self.cfg = A.load_config()
        self._show_region("Kaydedildi." + (" Çeviri yeniden başlatılıyor…" if self.runner.running else ""))
        if self.runner.running:
            self._restart = True
            self.runner.stop()

    # ------------------------------------------------------------ start / stop

    _restart = False

    def _start_stop(self) -> None:
        if self.runner.running:
            self.start_btn.config(state="disabled", text="Durduruluyor…")
            self.runner.stop()
            return
        if not self._save_settings(quiet=True):
            return
        if not self.cfg.get("tv_ip"):
            messagebox.showinfo("Başlat", "TV IP adresi boş. Ayarlar sekmesinden gir ya da 'Bul' ile ağda ara.")
            self.nb.select(1)
            return
        self._start()

    def _start(self) -> None:
        self.nb.select(0)
        self.runner.start(dict(self.cfg))
        self.start_btn.config(text="■  Durdur", style="TButton", state="normal")
        self.status_lbl.config(text="Başlatılıyor…", foreground=DIM)

    # ------------------------------------------------------------ connection buttons

    def _open_remote_play(self) -> None:
        if not B.open_remote_play():
            messagebox.showinfo("Remote Play", "PS Remote Play kurulu değil. 'İndir' ile Sony'nin sayfasından kur.")
        self._check_now()

    def _ps5_paired(self) -> str | None:
        """The paired PS5's name and PSN account (without asking the PS5); None if not paired."""
        try:
            import ps5_direct as D

            return D.paired_name()
        except Exception:
            return None

    def _pair_ps5(self) -> None:
        PairingWindow(self)

    def _find_tv(self) -> None:
        window = tk.Toplevel(self)
        window.title("TV'yi bul")
        window.geometry("520x340")
        window.transient(self)
        heading = ttk.Label(window, text="Ağ taranıyor… (birkaç saniye)", padding=12)
        heading.pack(anchor="w")
        entries = tk.Listbox(window, bg="#161616", fg="#e6e6e6", relief="flat", font=("Segoe UI", 11),
                           highlightthickness=0, selectbackground="#2f81f7")
        entries.pack(fill="both", expand=True, padx=12)
        conn_info = ttk.Label(window, text="", foreground=DIM, padding=12, wraplength=480)
        conn_info.pack(anchor="w")
        choose = ttk.Button(window, text="Seç", style="Accent.TButton", state="disabled")
        choose.pack(anchor="e", padx=12, pady=(0, 12))
        matches: list[dict] = []

        def finished(outcome):
            if not window.winfo_exists():
                return
            matches.extend(outcome)
            heading.config(text=f"{len(outcome)} cihaz bulundu." if outcome else "Hiç cihaz bulunamadı.")
            for r in outcome:
                if r.get("lg_dev"):
                    entries.insert("end", f"{r['ip']}    LG TV · geliştirici modu açık")
                elif r.get("lg"):
                    entries.insert("end", f"{r['ip']}    LG TV · geliştirici modu kapalı")
                elif r.get("samsung"):
                    state = "geliştirici modu açık" if r.get("samsung_dev") else "geliştirici modu kapalı"
                    entries.insert("end", f"{r['ip']}    Samsung TV {r.get('name') or ''} · {state}")
                elif r["uygulama"]:
                    entries.insert("end", f"{r['ip']}    Android TV · Dilmaç uygulaması açık")
                else:
                    entries.insert("end", f"{r['ip']}    Android cihaz (uygulama kurulu değil ya da kapalı)")
            if not outcome:
                conn_info.config(text="TV açık mı? Android TV'de uygulamayı aç ya da Geliştirici seçenekleri → "
                                  "USB hata ayıklama'yı aç; LG'de Developer Mode uygulamasını aç; Samsung'u aç. "
                                  "Sonra yeniden dene. "
                                  "IP'yi TV'nin ağ ayarlarından da görebilirsin.")
            else:
                entries.selection_set(0)
                choose.config(state="normal")

        def selected():
            i = entries.curselection()
            if not i:
                return
            self.v["tv_ip"].set(matches[i[0]]["ip"])
            self.v["tv_type"].set(matches[i[0]].get("kind", "android"))
            self._save_settings(quiet=True)
            window.destroy()

        choose.config(command=selected)
        entries.bind("<Double-Button-1>", lambda e: selected())
        threading.Thread(target=lambda: self.q.put(("cagir", {"fn": finished, "arg": B.scan_tvs()})), daemon=True).start()

    def _install_tv(self) -> None:
        ip = self.v["tv_ip"].get().strip()
        if not ip:
            messagebox.showinfo("TV'ye kur", "Önce TV'nin IP adresini gir ya da 'Bul' ile ara.")
            return
        if self.v["tv_type"].get() in ("lg", "samsung"):
            if self._save_settings(quiet=True):
                (LgSetupWindow if self.v["tv_type"].get() == "lg" else SamsungSetupWindow)(self)
            return
        if not messagebox.askokcancel(
                "TV'ye kur",
                f"Dilmaç uygulaması {ip} adresindeki TV'ye ağ üzerinden kurulacak.\n\n"
                "TV'de önceden: Ayarlar → Sistem → Hakkında → 'Android TV OS derlemesi'ne 7 kez bas; sonra "
                "Geliştirici seçenekleri → USB hata ayıklama'yı aç.\n\n"
                "TV ekranında 'Hata ayıklamaya izin verilsin mi?' sorusu çıkınca 'Her zaman' kutusunu işaretleyip izin ver."):
            return
        self.nb.select(0)
        self.lines_["tv"].adjust(B.Status("unknown", "Kuruluyor… (Canlı sekmesindeki günlüğe bak)"))

        def job():
            ok = B.install_tv(ip, A.log)
            self.q.put(("cagir", {"fn": lambda _: self._check_now(), "arg": ok}))

        threading.Thread(target=job, daemon=True).start()

    def _test_tv(self) -> None:
        ip, cfg = self.cfg.get("tv_ip", ""), dict(self.cfg)
        if cfg.get("tv_type") in A.WEB_TVS:
            A.log("TV'ye deneme yazısı gönderiliyor (uygulama kapalıysa önce açılır)…")

        def job():
            ok = B.send_tv_text(ip, "Dilmaç: bağlantı tamam. Türkçe altyazı İngilizcenin tam üstünde görünecek.",
                                cfg)
            A.log(f"TV'ye deneme yazısı {'gönderildi, TV ekranına bak' if ok else 'gönderilemedi'} ({ip}).")
            self._check_now()

        threading.Thread(target=job, daemon=True).start()

    def _try_font_size(self) -> None:
        """A sample subtitle on the TV with the size and opacity from Settings (not saved yet)."""
        try:
            px = max(16, min(80, int(self.v["font_px"].get())))
            opacity = max(0.3, min(1.0, int(self.v["box_opacity"].get()) / 100))
            fade = max(0, min(600, int(self.v["fade_ms"].get())))
        except (tk.TclError, ValueError):
            messagebox.showerror("Ayarlar", "Yazı boyutu bir sayı olmalı.")
            return
        ip = self.v["tv_ip"].get().strip() or self.cfg.get("tv_ip", "")
        cfg = {**self.cfg, "font_px": px, "box_opacity": opacity, "fade_ms": fade,
               "tv_type": self.v["tv_type"].get(), "tv_hdmi": int(self.v["tv_hdmi"].get() or 0)}

        def job():
            ok = B.send_tv_text(ip, f"Bu {px} punto. Lu, yavaşla biraz, polisler peşimizde!", cfg)
            A.log(f"TV'ye {px} punto örnek yazı {'gönderildi' if ok else 'gönderilemedi (TV açık mı?)'}.")

        threading.Thread(target=job, daemon=True).start()

    def _start_ollama(self) -> None:
        if not B.start_ollama():
            messagebox.showinfo("Ollama", "Ollama kurulu değil. ollama.com adresinden kurabilirsin.")
        self.after(2500, self._check_now)

    def _download_ollama(self) -> None:
        model = self.cfg.get("local_model", "")
        if not messagebox.askokcancel("Modeli indir", f"'{model}' modeli indirilecek (birkaç GB olabilir). Devam?"):
            return
        self.nb.select(0)

        def job():
            B.download_ollama(self.cfg.get("local_url", "http://127.0.0.1:11434"), model,
                           lambda m: self.q.put(("status", {"name": "engine", "status": B.Status("warn", f"İndiriliyor… {m}")})))
            self._check_now()

        threading.Thread(target=job, daemon=True).start()

    def _install_ocr(self) -> None:
        if messagebox.askokcancel("Yazı tanıma paketi",
                                  "Windows'un İngilizce yazı tanıma paketi kurulacak. Yönetici izni istenecek "
                                  "(Windows bir onay penceresi açar). Kurulum bitince bu pencereye dön."):
            B.install_ocr()
            self.after(5000, lambda: self._check_now(slow=True))

    def _fix_network(self) -> None:
        common = self.statuses.get("network", B.Status("", "")).data_.get("common", [])
        if not common:
            return
        if messagebox.askokcancel("Ağ profili",
                                  f"'{', '.join(common)}' ağı Ortak olarak işaretli; Windows bu durumda PS5 ve TV ile "
                                  "konuşmayı engelleyebilir. Ağ 'Özel' yapılacak (ev ağı için doğru seçim). "
                                  "Yönetici izni istenecek."):
            B.make_network_private(common)
            self.after(5000, lambda: self._check_now(slow=True))

    # ------------------------------------------------------------ checks

    def _check_now(self, ps5: bool = False, slow: bool = False) -> None:
        if ps5:
            self._ps5_last = 0.0
        if slow:
            self._slow_last = 0.0
        self._check_now_event.set()

    def _checker(self) -> None:
        """In the background: checks the connections every few seconds and puts the results in the queue."""
        def send_line(name, fn, *args):
            try:
                d = fn(*args)
            except Exception as e:
                d = B.Status("unknown", f"Denetlenemedi ({e})")
            self.q.put(("status", {"name": name, "status": d, "from_checker": True}))

        while not self._closing:
            cfg = self.cfg
            now_ = time.monotonic()
            slow = now_ - self._slow_last > 300
            # The fast ones first: every result goes to the screen as soon as it is ready.
            send_line("rp", self._rp_status, cfg)
            send_line("engine", self._engine_status, cfg)
            if slow:
                self._slow_last = now_
                send_line("ocr", B.check_ocr)
            if not self.runner.running or "tv" not in self.statuses:
                send_line("tv", B.check_tv, cfg.get("tv_ip", ""), cfg.get("tv_type", "android"))
            if now_ - self._ps5_last > 15:
                self._ps5_last = now_
                send_line("ps5", B.find_ps5, cfg.get("ps5_ip", ""))
            if slow:
                send_line("network", B.network_profile)
            self._check_now_event.wait(4)
            self._check_now_event.clear()

    @staticmethod
    def _rp_status(cfg) -> B.Status:
        if cfg.get("connection") == "direct":
            return B.Status("ok", "Gerekmiyor: PS5'e doğrudan bağlanılıyor (açıksa kapat)", {"window": True})
        matches = A.find_window(cfg["window_titles"])
        if matches:
            vr = A.video_rect(matches[0])
            if vr is None:
                return B.Status("warn", "Pencere simge durumunda; açık ve görünür kalmalı", {"window": True})
            return B.Status("ok", f"Pencere açık · görüntü {vr[2]}×{vr[3]}", {"window": True})
        if B.remote_play_exe() is None:
            return B.Status("bad", "Kurulu değil · gerekmez: PS5 satırındaki Eşle ile doğrudan bağlan",
                           {"installed": False})
        return B.Status("bad", "Kapalı · aç ya da PS5 satırındaki Eşle ile Remote Play'siz bağlan", {"installed": True})

    @staticmethod
    def _engine_status(cfg) -> B.Status:
        if cfg.get("google_only") or cfg.get("engine") == "google":
            return B.Status("warn", "Yalnız Google Çeviri (ücretsiz ama daha düz çeviri)", {"kind": "google"})
        if cfg.get("engine") == "gemini":
            d = B.check_gemini(cfg.get("gemini_api_key", ""))
            d.data_["kind"] = "gemini"
            return d
        d = B.check_ollama(cfg.get("local_url", "http://127.0.0.1:11434"), cfg.get("local_model", ""))
        d.data_["kind"] = "local"
        return d

    def _handle_status(self, name: str, d: B.Status) -> None:
        self.statuses[name] = d
        s = self.lines_[name]
        s.adjust(d)
        if name == "ps5":
            paired = self._ps5_paired()
            if paired:
                s.adjust(B.Status(d.level, f"{d.txt} · eşli ({paired})", d.data_))
            s.show_btn("search", "pair")
            ip = d.data_.get("ip")
            if ip and ip != self.cfg.get("ps5_ip"):
                self.v["ps5_ip"].set(ip)
                user = A.user_config()
                user["ps5_ip"] = ip
                A.save_config(user)
                self.cfg = A.load_config()
        elif name == "rp":
            s.show_btn() if d.data_.get("window") else s.show_btn("open" if d.data_.get("installed") else "download")
        elif name == "tv":
            s.show_btn("find", "install", "test") if d.level == "ok" else s.show_btn("find", "install")
        elif name == "engine":
            if d.data_.get("kind") == "local" and not d.data_.get("running", True):
                s.show_btn("start")
            elif d.data_.get("kind") == "local" and d.data_.get("model") is False:
                s.show_btn("download")
            else:
                s.show_btn()
        elif name == "ocr":
            s.show_btn() if d.level == "ok" else s.show_btn("install")
        elif name == "network":
            s.show_btn("correct") if d.data_.get("common") else s.show_btn()
        self._readiness_summary()

    def _readiness_summary(self) -> None:
        if self.runner.running:
            return
        missing = [label_text for name, label_text in (("tv", "TV"), ("engine", "çeviri motoru"), ("ocr", "yazı tanıma"))
                 if self.statuses.get(name) and self.statuses[name].level == "bad"]
        if missing:
            self.status_lbl.config(text="Eksik: " + ", ".join(missing), foreground=COLORS["warn"])
        elif self.cfg.get("connection") == "direct" and not self._ps5_paired():
            self.status_lbl.config(text="PS5 eşlenmedi (PS5 satırında Eşle)", foreground=COLORS["warn"])
        elif self.statuses.get("rp") and self.statuses["rp"].level != "ok":
            self.status_lbl.config(text="Remote Play bekleniyor", foreground=DIM)
        else:
            self.status_lbl.config(text="Hazır", foreground=COLORS["ok"])

    # ------------------------------------------------------------ event queue

    def _queue(self) -> None:
        try:
            for _ in range(200):
                event, data_ = self.q.get_nowait()
                self._on_event(event, data_)
        except queue.Empty:
            pass
        if not self._closing:
            self.after(80, self._queue)

    def _on_event(self, event: str, data_: dict) -> None:
        if event == "log":
            self._log(data_["line_count"])
        elif event == "status":
            # While translating, the TV status comes from the translator's own link; the background check must not override it.
            if not (data_.get("from_checker") and data_["name"] == "tv" and self.runner.running):
                self._handle_status(data_["name"], data_["status"])
        elif event == "cagir":
            data_["fn"](data_["arg"])
        elif event == "alan_sonuc":
            self._show_region(data_["txt"])
        elif event == "ready":
            self.status_lbl.config(text="Çalışıyor", foreground=COLORS["ok"])
        elif event == "window" and data_.get("direct"):
            status, err = data_.get("status", ""), data_.get("error", "")
            if status == "connected":
                self.status_lbl.config(text="Çalışıyor · PS5'ten görüntü geliyor", foreground=COLORS["ok"])
            elif status == "lost":
                self.status_lbl.config(text="PS5 bağlantısı koptu, yeniden deneniyor" + (f": {err[:60]}" if err else ""),
                                      foreground=COLORS["warn"])
            else:
                self.status_lbl.config(text="Çalışıyor · PS5'e bağlanılıyor", foreground=COLORS["warn"])
        elif event == "window":
            if data_.get("was_found"):
                self.status_lbl.config(text="Çalışıyor · altyazı bekleniyor", foreground=COLORS["ok"])
            else:
                self.status_lbl.config(text="Çalışıyor · Remote Play penceresi bekleniyor", foreground=COLORS["warn"])
            self._check_now()
        elif event == "tv":
            ip = self.cfg.get("tv_ip", "")
            lg = self.cfg.get("tv_type") in A.WEB_TVS
            label = {"lg": "LG TV, ", "samsung": "Samsung TV, "}.get(self.cfg.get("tv_type"), "")
            self._handle_status("tv", B.Status("ok", f"Bağlı ({label}{ip})", {"ip": ip})
                                if data_["connected"] else
                                B.Status("bad", f"TV bağlantısı yok ({ip}); " +
                                         ("TV'de uygulama açılıyor mu? Günlüğe bak" if lg else "TV'de uygulama açık mı?"),
                                         {"ip": ip}))
        elif event == "en":
            self.en_lbl.config(text=("✎ " if data_.get("correction") else "") + data_["txt"], foreground=DIM)
        elif event == "tr" and data_.get("track") == "objective":
            pass  # objective translations only in the log
        elif event == "tr":
            self.tr_lbl.config(text=data_["txt"], foreground="#ffffff")
            self.tr_info.config(text=f"{data_.get('source', '')} · {data_.get('ms', 0)} ms")
        elif event == "skipped":
            self.tr_info.config(text="Son satır anlamsız bulundu, gösterilmedi")
        elif event == "clear":
            self.tr_lbl.config(foreground="#6e7681")
            self.en_lbl.config(foreground="#4d5562")
        elif event == "error":
            messagebox.showerror("Dilmaç", data_["txt"])
        elif event == "stopped":
            self.start_btn.config(text="▶  Çeviriyi başlat", style="Accent.TButton", state="normal")
            self.status_lbl.config(text="Durduruldu", foreground=DIM)
            if self._restart and not self._closing:
                self._restart = False
                self._start()
            else:
                self._readiness_summary()

    def _close(self) -> None:
        self._closing = True
        self._check_now_event.set()
        if self.runner.running:
            self.runner.stop()
            self.runner.thread.join(timeout=3)
        self.destroy()


class LgSetupWindow(tk.Toplevel):
    """Installing the TV app on an LG TV once: the developer mode steps on the TV, then the passphrase; the PC fetches
    the TV's key, installs the app over SSH and opens it (lg_tv.setup)."""

    STEPS = (
        "1. Telefondan ya da bilgisayardan webostv.developer.lge.com adresinde ücretsiz bir LG hesabı aç "
        "(SIGN IN › Create Account).",
        "2. TV'de LG Content Store'dan 'Developer Mode' uygulamasını kur, aç ve bu hesapla giriş yap.",
        "3. 'Dev Mode Status'u aç. TV yeniden başlar.",
        "4. Developer Mode uygulamasını yeniden aç, 'Key Server'ı aç ve uygulamayı ekranda bırak.",
        "5. Uygulamanın sol altında yazan 6 karakterli Passphrase'i aşağıya yaz (büyük/küçük harf önemli) ve 'Kur'a bas.",
    )

    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        self.title("LG TV'ye kur")
        self.geometry("640x600")
        self.transient(app)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        wrap_px = 600
        ip = app.cfg.get("tv_ip", "")
        ttk.Label(f, text=f"LG TV: {ip}", font=("Segoe UI Semibold", 12)).pack(anchor="w")
        ttk.Label(f, text="Bir kerelik kurulum. Sonra uygulama çeviri başlayınca TV'de kendiliğinden açılır ve PS5 "
                          "görüntüsünü kendi içinde gösterir; altyazılar üstünde çıkar.",
                  foreground=DIM, wraplength=wrap_px, justify="left").pack(anchor="w", fill="x", pady=(2, 10))
        for step in self.STEPS:
            ttk.Label(f, text=step, wraplength=wrap_px, justify="left").pack(anchor="w", fill="x", pady=2)
        link = ttk.Label(f, text="webostv.developer.lge.com", foreground="#58a6ff", cursor="hand2",
                         font=("Segoe UI", 9, "underline"))
        link.pack(anchor="w", pady=(2, 0))
        link.bind("<Button-1>", lambda e: webbrowser.open("https://webostv.developer.lge.com"))
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(12, 0))
        ttk.Label(row, text="Passphrase").pack(side="left")
        self.passphrase = ttk.Entry(row, width=12, font=("Segoe UI", 13))
        self.passphrase.pack(side="left", padx=(10, 0))
        self.passphrase.bind("<Return>", lambda e: self._install())
        self.install_btn = ttk.Button(row, text="Kur", style="Accent.TButton", command=self._install)
        self.install_btn.pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Güvenlik duvarı izni", command=self._firewall).pack(side="right")
        self.out = tk.Text(f, height=8, wrap="word", bg="#161616", fg="#e6e6e6", relief="flat", font=("Consolas", 10),
                           padx=8, pady=6, borderwidth=0)
        self.out.pack(fill="both", expand=True, pady=(12, 0))
        ttk.Label(f, text="TV, PS5'in düğmesiyle kendi HDMI girişine geçip uygulamayı arkaya atıyorsa TV ayarlarında "
                          "SIMPLINK (HDMI-CEC) özelliğini kapat. Görüntü gelmezse TV kumandasında ◀ ▶ ile HDMI girişini "
                          "seç.", foreground=DIM, wraplength=wrap_px, justify="left").pack(anchor="w", fill="x",
                                                                                          pady=(10, 0))
        ttk.Button(f, text="Kapat", command=self.destroy).pack(side="bottom", anchor="e", pady=(10, 0))
        self.passphrase.focus_set()

    def _write(self, line: str) -> None:
        A.log(f"LG kurulumu: {line}")

        def add(_):
            if self.winfo_exists():
                self.out.insert("end", line + "\n")
                self.out.see("end")
        self.app.q.put(("cagir", {"fn": add, "arg": None}))

    def _install(self) -> None:
        import lg_tv

        cfg = dict(self.app.cfg)
        passphrase = self.passphrase.get().strip()
        if not passphrase:
            self._write("Önce TV'deki Passphrase'i yaz.")
            return
        self.install_btn.config(state="disabled")

        def job():
            try:
                hub = A.web_hub(cfg)  # listening before the app opens on the TV
            except OSError as e:
                hub = None
                self._write(f"TV için port açılamadı: {e}")
            ok = hub is not None and lg_tv.setup(cfg["tv_ip"], passphrase, int(cfg.get("tv_web_port") or 8788),
                                                 int(cfg.get("tv_hdmi") or 0), self._write)
            if ok:
                try:
                    self._write("TV'nin bu bilgisayara bağlanması bekleniyor…")
                    if hub.wait_client(25):
                        B.send_tv_text(cfg["tv_ip"], "Dilmaç LG TV'de hazır. Türkçe altyazılar burada görünecek.",
                                       cfg)
                        self._write("Tamam: TV bağlandı, ekranda bir deneme altyazısı görünmeli. Pencereyi kapatabilirsin.")
                    else:
                        self._write("Uygulama açıldı ama TV bu bilgisayara bağlanamadı. Windows güvenlik duvarı "
                                    "soru sorduysa izin ver ya da 'Güvenlik duvarı izni'ne bas; ağ profili 'Özel' olmalı.")
                except OSError as e:
                    self._write(f"TV için port açılamadı: {e}")

            def done(_):
                if self.winfo_exists():
                    self.install_btn.config(state="normal")
                self.app._check_now()
            self.app.q.put(("cagir", {"fn": done, "arg": None}))

        threading.Thread(target=job, daemon=True).start()

    def _firewall(self) -> None:
        port = int(self.app.cfg.get("tv_web_port") or 8788)
        if messagebox.askokcancel("Güvenlik duvarı", f"Windows güvenlik duvarına, TV'nin bu bilgisayarın {port} "
                                  "portuna ev ağından (Özel ağ) bağlanabilmesi için bir kural eklenecek. "
                                  "Yönetici izni istenecek.", parent=self):
            B.allow_web_tv_firewall(port)


class SamsungSetupWindow(tk.Toplevel):
    """Installing the TV app on a Samsung TV: developer mode with this PC as Host PC IP, then a Samsung sign-in in the
    browser (only when the TV has no certificate yet); the PC signs the app for the TV and installs it over sdb."""

    def __init__(self, app: "App"):
        super().__init__(app)
        import tv_web

        self.app = app
        self.title("Samsung TV'ye kur")
        self.geometry("660x640")
        self.transient(app)
        self._stop = threading.Event()
        self.protocol("WM_DELETE_WINDOW", self._close)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        wrap_px = 620
        ip = app.cfg.get("tv_ip", "")
        pc = tv_web.local_address_for(ip) if ip else ""
        ttk.Label(f, text=f"Samsung TV: {ip}", font=("Segoe UI Semibold", 12)).pack(anchor="w")
        ttk.Label(f, text="Bir kerelik kurulum. Samsung uygulamayı yalnız o TV için imzalanmışsa kurar; imzayı bu "
                          "bilgisayar senin ücretsiz Samsung hesabınla alır. Sonra uygulama çeviri başlayınca TV'de "
                          "kendiliğinden açılır ve PS5 görüntüsünü kendi içinde gösterir.",
                  foreground=DIM, wraplength=wrap_px, justify="left").pack(anchor="w", fill="x", pady=(2, 10))
        steps = (
            "1. TV'de Uygulamalar (Apps) ekranını aç ve kumandada sırayla 1 2 3 4 5'e bas.",
            f"2. Developer mode'u aç (On) ve Host PC IP'ye bu bilgisayarın adresini yaz: {pc or '?'}",
            "3. TV'yi tam yeniden başlat: güç tuşunu Samsung logosu görünene kadar (~5 sn) basılı tut ya da fişini 30 sn "
            "çek. Yalnız kapatıp açmak yetmez.",
            "4. Aşağıdaki 'Kur'a bas. Gerekirse tarayıcıda Samsung giriş sayfası açılır; giriş yap (hesabın yoksa "
            "orada ücretsiz açabilirsin). Tarayıcı yerel ağa erişim izni sorarsa izin ver.",
        )
        for step in steps:
            ttk.Label(f, text=step, wraplength=wrap_px, justify="left").pack(anchor="w", fill="x", pady=2)
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(12, 0))
        self.install_btn = ttk.Button(row, text="Kur", style="Accent.TButton", command=self._install)
        self.install_btn.pack(side="left")
        self.cancel_btn = ttk.Button(row, text="Girişi iptal et", command=self._stop.set, state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Güvenlik duvarı izni", command=self._firewall).pack(side="right")
        self.out = tk.Text(f, height=9, wrap="word", bg="#161616", fg="#e6e6e6", relief="flat", font=("Consolas", 10),
                           padx=8, pady=6, borderwidth=0)
        self.out.pack(fill="both", expand=True, pady=(12, 0))
        ttk.Label(f, text="TV, PS5'in düğmesiyle kendi HDMI girişine geçip uygulamayı arkaya atıyorsa TV ayarlarında "
                          "Anynet+ (HDMI-CEC) ve otomatik kaynak değiştirmeyi kapat (Ayarlar › Bağlantı › Harici Cihaz "
                          "Yöneticisi). Görüntü gelmezse TV kumandasında ◀ ▶ ile HDMI girişini seç.",
                  foreground=DIM, wraplength=wrap_px, justify="left").pack(anchor="w", fill="x", pady=(10, 0))
        ttk.Button(f, text="Kapat", command=self._close).pack(side="bottom", anchor="e", pady=(10, 0))

    def _close(self) -> None:
        self._stop.set()
        self.destroy()

    def _write(self, line: str) -> None:
        A.log(f"Samsung kurulumu: {line}")

        def add(_):
            if self.winfo_exists():
                self.out.insert("end", line + "\n")
                self.out.see("end")
        self.app.q.put(("cagir", {"fn": add, "arg": None}))

    def _install(self) -> None:
        import samsung_tv

        cfg = dict(self.app.cfg)
        self._stop.clear()
        self.install_btn.config(state="disabled")

        def ui(fn):
            self.app.q.put(("cagir", {"fn": lambda _: self.winfo_exists() and fn(), "arg": None}))

        def sign_in():
            ui(lambda: self.cancel_btn.config(state="normal"))
            try:
                return samsung_tv.sign_in(webbrowser.open, timeout=600, stop=self._stop)
            finally:
                ui(lambda: self.cancel_btn.config(state="disabled"))

        def job():
            try:
                hub = A.web_hub(cfg)  # listening before the app opens on the TV
            except OSError as e:
                hub = None
                self._write(f"TV için port açılamadı: {e}")
            ok = hub is not None and samsung_tv.install(cfg["tv_ip"], int(cfg.get("tv_web_port") or 8788),
                                                        self._write, sign_in)
            if ok:
                self._write("TV'nin bu bilgisayara bağlanması bekleniyor…")
                if hub.wait_client(30):
                    B.send_tv_text(cfg["tv_ip"], "Dilmaç Samsung TV'de hazır. Türkçe altyazılar burada görünecek.",
                                   cfg)
                    self._write("Tamam: TV bağlandı, ekranda bir deneme altyazısı görünmeli. Pencereyi kapatabilirsin.")
                else:
                    self._write("Uygulama açıldı ama TV bu bilgisayara bağlanamadı. Windows güvenlik duvarı soru "
                                "sorduysa izin ver ya da 'Güvenlik duvarı izni'ne bas; ağ profili 'Özel' olmalı.")

            def done():
                self.install_btn.config(state="normal")
                self.app._check_now()
            ui(done)

        threading.Thread(target=job, daemon=True).start()

    def _firewall(self) -> None:
        port = int(self.app.cfg.get("tv_web_port") or 8788)
        if messagebox.askokcancel("Güvenlik duvarı", f"Windows güvenlik duvarına, TV'nin bu bilgisayarın {port} "
                                  "portuna ev ağından (Özel ağ) bağlanabilmesi için bir kural eklenecek. "
                                  "Yönetici izni istenecek.", parent=self):
            B.allow_web_tv_firewall(port)


class PairingWindow(tk.Toplevel):
    """Pairing the PS5 once: PSN login in a small window (Sony's page; the redirect is caught by itself) + the PS5's
    8-digit code below it."""

    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        self.title("PS5'i eşle")
        self.geometry("600x470")
        self.transient(app)
        self._stop_login = threading.Event()
        self.protocol("WM_DELETE_WINDOW", self._close)
        f = ttk.Frame(self, padding=18)
        f.pack(fill="both", expand=True)
        wrap_px = 560

        def caption(txt, parent=f, **k):
            lbl = ttk.Label(parent, text=txt, wraplength=wrap_px, justify="left", **k)
            lbl.pack(anchor="w", fill="x")
            return lbl

        caption("Bir kerelik eşleme. Sonra Remote Play uygulaması gerekmez: uygulama PS5'e kendisi bağlanır, "
                "dinlenme modundaysa uyandırır.", foreground=DIM)

        ttk.Label(f, text="1. PSN hesabı", font=("Segoe UI Semibold", 12)).pack(anchor="w", pady=(14, 2))
        caption("Küçük bir Sony giriş penceresi açılır; ikinci PSN hesabınla giriş yap. Şifren yalnız Sony'ye gider. "
                "Giriş bitince pencere kendiliğinden kapanır.")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(8, 0))
        self.login_btn = ttk.Button(row, text="PSN ile giriş yap", style="Accent.TButton", command=self._login)
        self.login_btn.pack(side="left")
        self.account_lbl = ttk.Label(row, text="", foreground=COLORS["ok"])
        self.account_lbl.pack(side="left", padx=12)

        # Fallback: the system browser + pasting the address (no Edge/Chrome, or the small window failed).
        self.manual = ttk.Frame(f)
        caption("Tarayıcıda giriş yap; giriş bitince adres çubuğundaki adresin tamamını (remoteplay.dl.playstation.net/... "
                "ile başlar) buraya yapıştır.", parent=self.manual, foreground=DIM)
        mrow = ttk.Frame(self.manual)
        mrow.pack(fill="x", pady=(6, 0))
        ttk.Button(mrow, text="Tarayıcıda aç", command=self._open_login).pack(side="left")
        self.address = ttk.Entry(mrow)
        self.address.pack(side="left", fill="x", expand=True, padx=(8, 0))
        ttk.Button(mrow, text="Yapıştır", command=self._paste).pack(side="left", padx=(8, 0))
        self.account_btn = ttk.Button(mrow, text="Hesabı ekle", command=self._add_account)
        self.account_btn.pack(side="left", padx=(8, 0))
        self.manual_link = ttk.Label(f, text="Giriş penceresi açılmıyor mu? Tarayıcıda giriş yap", foreground=DIM,
                                     cursor="hand2", font=("Segoe UI", 9, "underline"))
        self.manual_link.pack(anchor="w", pady=(6, 0))
        self.manual_link.bind("<Button-1>", lambda e: self._show_manual())

        ttk.Separator(f).pack(fill="x", pady=(14, 0))
        ttk.Label(f, text="2. PS5'i eşle", font=("Segoe UI Semibold", 12)).pack(anchor="w", pady=(10, 2))
        caption("PS5'te Ayarlar > Sistem > Uzaktan Oynatma > Cihazı Bağla'yı aç ve ekrandaki 8 haneli kodu yaz. "
                "Resmi Remote Play uygulaması açıksa önce kapat (PS5 aynı anda tek bağlantı kabul eder).")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(8, 0))
        self.pin = ttk.Entry(row, width=14, font=("Segoe UI", 13))
        self.pin.pack(side="left")
        self.pin.bind("<Return>", lambda e: self._pair())
        self.pair_btn = ttk.Button(row, text="Eşle", style="Accent.TButton", command=self._pair)
        self.pair_btn.pack(side="left", padx=(8, 0))
        self.ps5_lbl = ttk.Label(row, text=f"PS5: {app.cfg.get('ps5_ip') or 'IP bilinmiyor (PS5 satırında Ara)'}",
                                 foreground=DIM)
        self.ps5_lbl.pack(side="left", padx=12)

        self.status = ttk.Label(f, text="", wraplength=wrap_px, justify="left")
        self.status.pack(anchor="w", fill="x", pady=(14, 0))
        ttk.Button(f, text="Kapat", command=self._close).pack(side="bottom", anchor="e")
        self._show_account()

    def _close(self) -> None:
        self._stop_login.set()  # an open login window is closed by its thread
        self.destroy()

    def _show_manual(self) -> None:
        self.manual_link.pack_forget()
        self.manual.pack(fill="x", pady=(8, 0), after=self.login_btn.master)

    def _login(self) -> None:
        import ps5_direct as D
        import psn_login as L

        if L.find_browser() is None:
            self._write("Microsoft Edge bulunamadı; tarayıcıda giriş yapıp adresi yapıştır.", COLORS["warn"])
            self._show_manual()
            return
        self._stop_login.clear()
        self._write("Sony giriş penceresi açılıyor… Giriş bitince kendiliğinden kapanır.", DIM)

        def on_done(name):
            self.account_lbl.config(text=f"Hesap: {name} ✓")
            self._write(f"Hesap eklendi: {name}. Şimdi PS5'teki 8 haneli kodu gir.", COLORS["ok"])
            self.pin.focus_set()

        def job():
            try:
                address = L.sign_in(D.login_url(), D._REDIRECT, stop=self._stop_login)
            except L.LoginClosed:
                raise D.PS5Error("Giriş penceresi kapatıldı; yeniden dene.")
            except OSError as e:
                raise D.PS5Error(f"Giriş penceresi açılamadı ({e}). Tarayıcıda giriş yapıp adresi yapıştır.")
            return D.add_account(address)

        self._in_background(job, on_done, self.login_btn)

    def _write(self, txt: str, color: str = "#e6e6e6") -> None:
        if self.winfo_exists():
            self.status.config(text=txt, foreground=color)

    def _show_account(self) -> None:
        def job():
            try:
                import ps5_direct as D

                names = D.accounts()
            except Exception:
                names = []
            self.app.q.put(("cagir", {"fn": lambda a: self.winfo_exists() and self.account_lbl.config(
                text=f"Hesap: {', '.join(a)} ✓" if a else ""), "arg": names}))
        threading.Thread(target=job, daemon=True).start()

    def _open_login(self) -> None:
        import ps5_direct as D

        webbrowser.open(D.login_url())
        self._write("Tarayıcıda giriş yap; sonra adres çubuğundaki adresi buraya yapıştır.", DIM)

    def _paste(self) -> None:
        try:
            txt = self.clipboard_get()
        except tk.TclError:
            txt = ""
        self.address.delete(0, "end")
        self.address.insert(0, txt.strip())

    def _in_background(self, fn, on_done, btn: ttk.Button) -> None:
        btn.config(state="disabled")

        def job():
            try:
                outcome, err = fn(), None
            except Exception as e:  # PS5Error included: its message is in Turkish
                outcome, err = None, str(e)
            self.app.q.put(("cagir", {"fn": lambda _: self._ended(outcome, err, on_done, btn), "arg": None}))
        threading.Thread(target=job, daemon=True).start()

    def _ended(self, outcome, err, on_done, btn) -> None:
        if not self.winfo_exists():
            return
        btn.config(state="normal")
        if err:
            self._write(err, COLORS["bad"])
        else:
            on_done(outcome)

    def _add_account(self) -> None:
        import ps5_direct as D

        txt = self.address.get()
        self._write("Sony'den hesap bilgisi alınıyor…", DIM)

        def on_done(name):
            self.account_lbl.config(text=f"Hesap: {name} ✓")
            self._write(f"Hesap eklendi: {name}. Şimdi PS5'teki kodu gir.", COLORS["ok"])
        self._in_background(lambda: D.add_account(txt), on_done, self.account_btn)

    def _pair(self) -> None:
        import ps5_direct as D

        ip = self.app.cfg.get("ps5_ip", "")
        if not ip:
            self._write("PS5'in IP adresi bilinmiyor. Bağlantılar'da PS5 satırındaki 'Ara' ile bul ya da Ayarlar'a yaz.",
                      COLORS["bad"])
            return
        pin = self.pin.get()
        self._write("PS5'le eşleniyor…", DIM)
        D.attach_log(A.log)  # chiaki's warnings go to the log in the Live tab

        def on_done(name):
            user = A.user_config()
            user["connection"] = "direct"
            new_ip = (D.paired_ps5() or {}).get("host")
            if new_ip:  # if pairing found the PS5 at another address
                user["ps5_ip"] = new_ip
            A.save_config(user)
            self.app.cfg = A.load_config()
            self.app.v["connection"].set("direct")
            self._write(f"Eşlendi: {name}. Görüntü kaynağı 'Doğrudan PS5' yapıldı; artık Remote Play'i açmadan "
                      "'Çeviriyi başlat'a basabilirsin.", COLORS["ok"])
            self.app._check_now(ps5=True)
        self._in_background(lambda: D.pair(ip, pin), on_done, self.pair_btn)


def main() -> None:
    import sys

    A.set_dpi_aware()
    app = App()
    if "--start" in sys.argv[1:] or "--baslat" in sys.argv[1:]:  # --baslat: older name
        # After an update, when reopened, translation continues where it left off (during play).
        app.after(1500, app._start_stop)
    app.mainloop()


if __name__ == "__main__":
    main()
