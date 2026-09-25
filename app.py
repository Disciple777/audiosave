"""AudioSave - desktop app that records your computer's audio.

Captures system audio (what you hear through your speakers, via WASAPI
loopback) and/or your microphone, mixes them with per-source volume
control, and saves an MP3 (default) and/or WAV file.

Run:  python app.py
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from recorder import (
    AudioEngine,
    CaptureThread,
    mix_and_encode,
    mp3_available,
    repair_wav_header,
    wav_data_bytes,
)

# Frozen (PyInstaller) builds run from a temp folder that Windows deletes, so
# anchor the recordings folder to the .exe itself rather than to this file.
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
RECORDINGS_DIR = APP_DIR / "recordings"
# In-progress capture files live here until the final MP3/WAV is written.
# Anything left behind (crash, power cut, killed process) is recovered on the
# next launch - never deleted - so no recording can be silently lost.
TMP_DIR = RECORDINGS_DIR / ".tmp"
# How long a capture thread may take to finish after Stop before we mix
# whatever it wrote anyway (a hung audio device must not block saving).
STOP_GRACE_SECS = 8.0
# Leave temp files this recently written alone at startup: another AudioSave
# window may still be recording into them. They're re-checked a bit later.
RECOVERY_MIN_AGE_SECS = 30.0

# ---- palette ------------------------------------------------------------- #
BG = "#0f1117"
PANEL = "#171a22"
PANEL_2 = "#1e222c"
FG = "#e6e9ef"
FG_MUTED = "#8b93a5"
ACCENT = "#4f8cff"
ACCENT_HOVER = "#6ea0ff"
GREEN = "#35c98e"
RED = "#ff5d5d"
AMBER = "#f5c542"
BORDER = "#262b36"

FMT_OPTIONS = [("MP3 (recommended)", "mp3"), ("WAV", "wav"), ("MP3 + WAV", "both")]


def fmt_bytes(n: int) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def validate_rename_name(name: str) -> str | None:
    """Return an error message if ``name`` can't be used as a file name on
    Windows, else None (valid). Pure function so it can be unit-tested."""
    name = strip_audio_ext(name.strip())
    if not name:
        return "Name can't be empty."
    if any(ch in name for ch in '<>:"/\\|?*'):
        return 'Name contains invalid characters: < > : " / \\ | ? *'
    if any(ord(ch) < 32 for ch in name):
        return "Name can't contain control characters."
    if name.endswith("."):
        return "Name can't end with a dot."
    stem = name.split(".")[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL"} or (
        len(stem) >= 4 and stem[:3] in {"COM", "LPT"} and stem[3:].isdigit()
    ):
        return f"'{stem}' is a reserved Windows name."
    return None


AUDIO_EXTS = (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac")


def strip_audio_ext(name: str) -> str:
    """Drop an audio extension the user typed (the file keeps its real one)."""
    for ext in AUDIO_EXTS:
        if name.lower().endswith(ext):
            return name[: -len(ext)].strip()
    return name


class VUMeter(tk.Canvas):
    """Segmented live level meter."""

    def __init__(self, master, **kw):
        super().__init__(master, height=16, bg=PANEL_2, highlightthickness=0, **kw)
        self.level = 0.0

    def set(self, level: float) -> None:
        self.level = max(0.0, min(1.0, level))

    def draw(self) -> None:
        c = self
        w = c.winfo_width()
        if w <= 1:
            w = 300
        h = c.winfo_height() or 16
        c.delete("all")
        c.create_rectangle(0, 0, w, h, fill=PANEL_2, outline="")
        fill_w = int(w * self.level)
        if fill_w > 0:
            seg = 4
            x = 0
            while x < fill_w:
                frac = x / max(1.0, w)
                color = GREEN if frac < 0.55 else (AMBER if frac < 0.85 else RED)
                c.create_rectangle(
                    x, 1, min(x + seg - 1, fill_w), h - 1, fill=color, outline=""
                )
                x += seg


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("AudioSave")
        self.geometry("980x680")
        self.minsize(820, 600)
        self.configure(bg=BG)

        try:
            self._engine = AudioEngine()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(
                "AudioSave",
                f"Could not initialize audio: {exc}\n\n"
                "Make sure you are on Windows with audio devices available.",
            )
            self.destroy()
            return
        self._threads: list[CaptureThread] = []
        self._status_q: queue.Queue = queue.Queue()
        self._level_sys = 0.0
        self._level_mic = 0.0
        self._started_at: float | None = None
        self._timer_job: str | None = None
        self._stamp = ""
        self._output_devices: list[dict] = []
        self._input_devices: list[dict] = []
        self._rec_files: list[Path] = []
        # gain envelopes: (seconds since record start, gain) per source, so
        # slider changes made mid-recording are baked into the track.
        self._env_sys: list[tuple[float, float]] = []
        self._env_mic: list[tuple[float, float]] = []
        self._active_sources: set[str] = set()
        self._stop_deadline: float | None = None
        # Stamps whose audio is being mixed/saved right now (incl. recovery).
        self._saving: set[str] = set()
        self._closing = False

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        RECORDINGS_DIR.mkdir(exist_ok=True)
        TMP_DIR.mkdir(exist_ok=True)

        self._configure_styles()
        self._build_ui()
        self.refresh_devices()
        self.refresh_recordings()
        self.bind("<space>", self._on_space)
        self.after(120, self._poll)
        # Finish saving anything a previous session left unsaved (crash,
        # killed process, power cut...). Temp files are never just deleted.
        self.after(500, self._recover_interrupted)

    # ---- styles ----------------------------------------------------------- #
    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "TCombobox",
            fieldbackground=PANEL_2,
            background=PANEL_2,
            foreground=FG,
            arrowcolor=FG_MUTED,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
        )
        style.map("TCombobox", fieldbackground=[("readonly", PANEL_2)])
        style.configure("TCheckbutton", background=PANEL, foreground=FG)
        style.map("TCheckbutton", background=[("active", PANEL)])

    # ---- layout ----------------------------------------------------------- #
    def _build_ui(self) -> None:
        root = tk.Frame(self, bg=BG)
        root.pack(fill="both", expand=True, padx=18, pady=16)

        header = tk.Frame(root, bg=BG)
        header.pack(fill="x", pady=(0, 14))
        tk.Label(
            header, text="AudioSave", font=("Segoe UI", 20, "bold"), bg=BG, fg=FG
        ).pack(side="left")
        tk.Label(
            header, text="Record system audio + microphone",
            font=("Segoe UI", 10), bg=BG, fg=FG_MUTED,
        ).pack(side="left", padx=(12, 0), pady=(6, 0))

        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True)

        left = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self._build_source_panel(left)

        right = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self._build_recordings_panel(right)

    def _build_source_panel(self, parent: tk.Frame) -> None:
        tk.Label(
            parent, text="Sources", font=("Segoe UI", 12, "bold"), bg=PANEL, fg=FG
        ).pack(anchor="w", padx=16, pady=(16, 6))

        self.sys_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            parent, text="System audio (speakers)", variable=self.sys_var,
            command=self._on_toggle_sys,
        ).pack(anchor="w", padx=16, pady=(4, 0))
        self.sys_combo = ttk.Combobox(parent, state="readonly", width=40)
        self.sys_combo.pack(fill="x", padx=16, pady=(2, 10))

        self.mic_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            parent, text="Microphone", variable=self.mic_var, command=self._on_toggle_mic
        ).pack(anchor="w", padx=16)
        self.mic_combo = ttk.Combobox(parent, state="readonly", width=40)
        self.mic_combo.pack(fill="x", padx=16, pady=(2, 10))

        # ---- levels & volume ---- #
        tk.Label(
            parent, text="Levels & Volume", font=("Segoe UI", 12, "bold"),
            bg=PANEL, fg=FG,
        ).pack(anchor="w", padx=16, pady=(6, 4))
        self._build_meter_row(parent, "Desktop", "sys")
        self._build_meter_row(parent, "Mic", "mic")

        # ---- format ---- #
        fmt_row = tk.Frame(parent, bg=PANEL)
        fmt_row.pack(fill="x", padx=16, pady=(12, 4))
        tk.Label(fmt_row, text="Save as:", bg=PANEL, fg=FG_MUTED).pack(side="left")
        self.fmt_combo = ttk.Combobox(fmt_row, state="readonly", width=16)
        self.fmt_combo.pack(side="left", padx=(8, 0))

        self.time_label = tk.Label(
            parent, text="00:00:00", font=("Consolas", 24, "bold"), bg=PANEL, fg=FG
        )
        self.time_label.pack(anchor="center", pady=(14, 6))
        self.record_btn = tk.Button(
            parent, text="\u25cf  Record", command=self._toggle_record,
            bg=ACCENT, fg="#ffffff", activebackground=ACCENT_HOVER,
            activeforeground="#ffffff", relief="flat", bd=0, cursor="hand2",
            font=("Segoe UI", 12, "bold"), padx=30, pady=10,
        )
        self.record_btn.pack(pady=(0, 8))

        self.status_label = tk.Label(
            parent, text="Ready", font=("Segoe UI", 9), bg=PANEL, fg=FG_MUTED,
            wraplength=420, justify="left",
        )
        self.status_label.pack(side="bottom", fill="x", padx=16, pady=12)

    def _build_meter_row(self, parent: tk.Frame, name: str, key: str) -> None:
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", padx=16, pady=2)

        tk.Label(
            row, text=name, width=7, anchor="w", bg=PANEL, fg=FG, font=("Segoe UI", 9)
        ).pack(side="left")

        meter = VUMeter(row)
        meter.pack(side="left", fill="x", expand=True, padx=(0, 8))
        setattr(self, f"_meter_{key}", meter)

        pct = tk.Label(row, text="100%", width=4, bg=PANEL, fg=FG_MUTED)
        pct.pack(side="right")

        def _on_slide(val: str) -> None:
            gain = float(val) / 100.0
            pct.configure(text=f"{int(float(val))}%")
            self._record_gain(key, gain)
            if self._started_at is not None:
                self._draw_meter(key)

        scale = tk.Scale(
            row, from_=0, to=300, resolution=5, orient="horizontal", length=150,
            showvalue=False, command=_on_slide, bg=PANEL, fg=FG, troughcolor=PANEL_2,
            highlightthickness=0, activebackground=BORDER, bd=0, cursor="hand2",
            font=("Segoe UI", 8),
        )
        scale.set(100)
        scale.pack(side="right")
        setattr(self, f"_gain_{key}", scale)
        scale.configure(takefocus=0)

    def _build_recordings_panel(self, parent: tk.Frame) -> None:
        head = tk.Frame(parent, bg=PANEL)
        head.pack(fill="x", padx=16, pady=(16, 10))
        tk.Label(
            head, text="Recordings", font=("Segoe UI", 12, "bold"), bg=PANEL, fg=FG
        ).pack(side="left")
        tk.Button(
            head, text="\u21bb", command=self.refresh_recordings, bg=PANEL_2, fg=FG,
            relief="flat", cursor="hand2", font=("Segoe UI", 11),
        ).pack(side="right")

        self.rec_list = tk.Listbox(
            parent, bg=PANEL_2, fg=FG, selectbackground=ACCENT, selectforeground="#fff",
            relief="flat", highlightthickness=0, font=("Segoe UI", 10),
            activestyle="none", bd=0,
        )
        self.rec_list.pack(fill="both", expand=True, padx=16)
        self.rec_list.bind("<Double-Button-1>", lambda _e: self._play_selected())

        # Right-click context menu: play / rename / delete.
        self._rec_menu = tk.Menu(
            parent, tearoff=0, bg=PANEL_2, fg=FG,
            activebackground=ACCENT, activeforeground="#ffffff",
        )
        self._rec_menu.add_command(label="\u25b6  Play", command=self._play_selected)
        self._rec_menu.add_command(
            label="\u270e  Rename\u2026", command=self._rename_selected
        )
        self._rec_menu.add_separator()
        self._rec_menu.add_command(label="\u2715  Delete", command=self._delete_selected)
        self.rec_list.bind("<Button-3>", self._on_list_right_click)

        self.rec_list.bind("<F2>", lambda _e: self._rename_selected())

        btns = tk.Frame(parent, bg=PANEL)
        btns.pack(fill="x", padx=16, pady=12)
        self._mk_btn(btns, "\u25b6 Play", self._play_selected).pack(side="left", padx=(0, 6))
        self._mk_btn(btns, "\u270e Rename", self._rename_selected).pack(side="left", padx=6)
        self._mk_btn(btns, "\U0001f4c1 Folder", self._open_folder).pack(side="left", padx=6)
        self._mk_btn(btns, "\u2715 Delete", self._delete_selected).pack(side="left", padx=6)

    def _mk_btn(
        self, parent: tk.Frame, text: str, cmd, primary: bool = False
    ) -> tk.Button:
        return tk.Button(
            parent, text=text, command=cmd,
            bg=ACCENT if primary else PANEL_2,
            fg="#ffffff" if primary else FG,
            activebackground=ACCENT_HOVER if primary else BORDER,
            activeforeground="#ffffff" if primary else FG,
            relief="flat", bd=0, cursor="hand2",
            font=("Segoe UI", 10), padx=14, pady=6,
        )

    # ---- devices & recordings --------------------------------------------- #
    def refresh_devices(self) -> None:
        self._output_devices = self._engine.output_devices()
        self._input_devices = self._engine.input_devices()

        out_names = [d["name"] for d in self._output_devices]
        in_names = [d["name"] for d in self._input_devices]
        self.sys_combo.configure(values=out_names)
        self.mic_combo.configure(values=in_names)

        default = self._engine.default_output()
        if default and default["name"] in out_names:
            self.sys_combo.set(default["name"])
        elif out_names:
            self.sys_combo.set(out_names[0])

        if in_names:
            self.mic_combo.set(in_names[0])

        # format selector
        if mp3_available():
            self._fmt_options = FMT_OPTIONS
        else:
            self._fmt_options = [("WAV", "wav")]
        self.fmt_combo.configure(values=[lbl for lbl, _ in self._fmt_options])
        self.fmt_combo.current(0)

    def refresh_recordings(self) -> None:
        self.rec_list.delete(0, "end")
        files = sorted(
            (
                list(RECORDINGS_DIR.glob("*.wav")) + list(RECORDINGS_DIR.glob("*.mp3"))
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for p in files:
            self.rec_list.insert("end", f"{p.name}   ({fmt_bytes(p.stat().st_size)})")
        self._rec_files = files

    # ---- source toggles --------------------------------------------------- #
    def _on_toggle_sys(self) -> None:
        self.sys_combo.configure(
            state="readonly" if self.sys_var.get() else "disabled"
        )

    def _on_toggle_mic(self) -> None:
        self.mic_combo.configure(
            state="readonly" if self.mic_var.get() else "disabled"
        )

    # ---- recording -------------------------------------------------------- #
    def _on_space(self, _event=None) -> None:
        # For key events, _event.widget is the focused widget. If a widget that
        # itself consumes the space key (button, checkbox, combo, listbox) has
        # focus, its own class binding already handled the keypress - only
        # toggle when the root window or an inert widget (e.g. a slider, which
        # ignores space) is focused, otherwise we'd toggle twice (e.g. stop a
        # recording and instantly start a new one).
        if _event is not None and _event.widget is not self:
            w = _event.widget
            if isinstance(w, (tk.Button, ttk.Checkbutton, ttk.Combobox, tk.Listbox)):
                return
        self._toggle_record()

    def _toggle_record(self) -> None:
        if self._threads:
            self._request_stop()
            return
        self._start_recording()

    def _request_stop(self) -> None:
        """Tell the capture threads to finish; _tick saves once they have (or
        after STOP_GRACE_SECS, if an audio device hangs). Never blocks the UI."""
        if self._stop_deadline is None:
            self._stop_deadline = time.monotonic() + STOP_GRACE_SECS
            self.record_btn.configure(text="Saving...", state="disabled")
        for t in self._threads:
            t.request_stop()

    def _capture_finished(self) -> bool:
        if not self._threads:
            return False
        if all(not t.is_alive() for t in self._threads):
            return True
        return self._stop_deadline is not None and time.monotonic() > self._stop_deadline

    # ---- temp-file bookkeeping -------------------------------------------- #
    @staticmethod
    def _tmp_paths(stamp: str) -> tuple[Path, Path, Path]:
        return (
            TMP_DIR / f"{stamp}_sys.wav",
            TMP_DIR / f"{stamp}_mic.wav",
            TMP_DIR / f"{stamp}.json",
        )

    def _save_meta(self) -> None:
        """Persist what's needed to finish this recording (format + volume
        automation) next to its capture files, so crash recovery produces the
        same mix the user would have got."""
        meta = {"fmt": self._current_fmt(), "env_sys": self._env_sys, "env_mic": self._env_mic}
        path = self._tmp_paths(self._stamp)[2]
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(meta), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass  # best effort - recovery falls back to 100% volume

    def _current_fmt(self) -> str:
        idx = self.fmt_combo.current()
        if 0 <= idx < len(self._fmt_options):
            return self._fmt_options[idx][1]
        return "mp3"

    def _start_recording(self) -> None:
        speaker = None
        mic = None
        if self.sys_var.get() and 0 <= self.sys_combo.current() < len(self._output_devices):
            speaker = self._output_devices[self.sys_combo.current()]
        if self.mic_var.get() and 0 <= self.mic_combo.current() < len(self._input_devices):
            mic = self._input_devices[self.mic_combo.current()]
        if speaker is None and mic is None:
            messagebox.showwarning("AudioSave", "Pick at least one source to record.")
            return

        self._stamp = datetime.now().strftime("rec_%Y-%m-%d_%H-%M-%S")
        TMP_DIR.mkdir(exist_ok=True)
        self._threads = []
        self._active_sources = set()
        if speaker is not None:
            self._env_sys = [(0.0, self._gain_sys.get() / 100.0)]
            self._active_sources.add("sys")
            t = CaptureThread(
                TMP_DIR / f"{self._stamp}_sys.wav", "system", speaker,
                pa=self._engine.pa,
                on_level=self._set_level_sys, on_status=self._status_q.put,
            )
            t.start()
            self._threads.append(t)
        else:
            self._env_sys = []
        if mic is not None:
            self._env_mic = [(0.0, self._gain_mic.get() / 100.0)]
            self._active_sources.add("mic")
            t = CaptureThread(
                TMP_DIR / f"{self._stamp}_mic.wav", "mic", mic,
                pa=self._engine.pa,
                on_level=self._set_level_mic, on_status=self._status_q.put,
            )
            t.start()
            self._threads.append(t)
        else:
            self._env_mic = []
        self._save_meta()

        self._stop_deadline = None
        self.record_btn.configure(text="\u25a0  Stop", bg=RED, activebackground="#ff8080")
        self.sys_combo.configure(state="disabled")
        self.mic_combo.configure(state="disabled")
        self._started_at = time.monotonic()
        self._timer_job = self.after(250, self._tick)
        self.status_label.configure(text="Recording...", fg=GREEN)

    def _tick(self) -> None:
        if self._started_at is not None:
            secs = int(time.monotonic() - self._started_at)
            self.time_label.configure(
                text=f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"
            )
            self._draw_meter("sys")
            self._draw_meter("mic")
        if self._capture_finished():
            self._finalize_recording()
            return
        self._timer_job = self.after(250, self._tick)

    def _finalize_recording(self) -> None:
        threads, self._threads = self._threads, []
        if self._timer_job:
            self.after_cancel(self._timer_job)
            self._timer_job = None
        self._started_at = None
        self._stop_deadline = None

        self.record_btn.configure(
            text="\u25cf  Record", bg=ACCENT, activebackground=ACCENT_HOVER, state="normal"
        )
        self.sys_combo.configure(state="readonly" if self.sys_var.get() else "disabled")
        self.mic_combo.configure(state="readonly" if self.mic_var.get() else "disabled")
        self.time_label.configure(text="00:00:00")
        self._meter_sys.set(0.0)
        self._meter_mic.set(0.0)
        self._meter_sys.draw()
        self._meter_mic.draw()

        for t in threads:
            t.request_stop()  # a hung thread past the grace period - mix what it wrote

        errors = [t.error for t in threads if t.error is not None]
        total = sum(t.total_frames for t in threads)
        if total == 0:
            if errors:
                self.status_label.configure(text=f"Error: {errors[0]}", fg=RED)
            else:
                self.status_label.configure(
                    text="Nothing recorded (stopped too quickly).", fg=FG_MUTED
                )
            self._cleanup_tmp()
            return
        if errors:
            # One source failed but the other captured something - mix what
            # exists and warn instead of discarding the good track.
            self.status_label.configure(
                text=f"Note: {errors[0]} - saving the captured audio...", fg=AMBER
            )
        else:
            self.status_label.configure(text="Saving (mixing)...", fg=FG_MUTED)

        self._save_meta()  # final envelopes + format, in case we crash while mixing
        self._start_save(
            self._stamp, list(self._env_sys), list(self._env_mic), self._current_fmt()
        )

    def _start_save(
        self,
        stamp: str,
        env_sys: list[tuple[float, float]],
        env_mic: list[tuple[float, float]],
        fmt: str,
        recovered: bool = False,
    ) -> None:
        self._saving.add(stamp)
        # Not a daemon: if the window closes mid-save, Python still waits for
        # the file to be finished before exiting.
        threading.Thread(
            target=self._mix_worker,
            args=(stamp, env_sys, env_mic, fmt, recovered),
            daemon=False,
        ).start()

    def _mix_worker(
        self,
        stamp: str,
        env_sys: list[tuple[float, float]],
        env_mic: list[tuple[float, float]],
        fmt: str,
        recovered: bool,
    ) -> None:
        sys_tmp, mic_tmp, meta = self._tmp_paths(stamp)
        try:
            created = mix_and_encode(
                sys_tmp if sys_tmp.exists() else None,
                mic_tmp if mic_tmp.exists() else None,
                env_sys, env_mic, RECORDINGS_DIR / stamp, fmt,
            )
            if not created or not all(p.exists() and p.stat().st_size > 0 for p in created):
                raise RuntimeError("the output file was not written")
        except Exception as exc:  # noqa: BLE001
            # Keep the audio no matter what: hand the raw tracks to the user.
            rescued = self._rescue_raw_tracks(stamp)
            self._status_q.put(("error", (stamp, str(exc), rescued)))
            return
        # Only now - with the final file verified on disk - drop the temp audio.
        for p in (sys_tmp, mic_tmp, meta):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        self._status_q.put(("done", (stamp, created, recovered)))

    def _rescue_raw_tracks(self, stamp: str) -> list[Path]:
        """Mixing failed: move the raw capture WAVs into the recordings folder
        (e.g. ``rec_..._desktop.wav``) so the audio is never lost. Anything
        that can't be moved stays in .tmp and is retried on next launch."""
        sys_tmp, mic_tmp, meta = self._tmp_paths(stamp)
        rescued: list[Path] = []
        for src, label in ((sys_tmp, "desktop"), (mic_tmp, "mic")):
            if not src.exists():
                continue
            if wav_data_bytes(src) == 0:
                src.unlink(missing_ok=True)
                continue
            dst = RECORDINGS_DIR / f"{stamp}_{label}.wav"
            try:
                try:
                    repair_wav_header(src)
                except Exception:
                    pass
                os.replace(src, dst)
                rescued.append(dst)
            except OSError:
                pass
        if not sys_tmp.exists() and not mic_tmp.exists():
            meta.unlink(missing_ok=True)
        return rescued

    def _cleanup_tmp(self) -> None:
        for p in self._tmp_paths(self._stamp):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

    # ---- crash recovery ---------------------------------------------------- #
    def _recover_interrupted(self) -> None:
        """Save any recording a previous session didn't finish (it crashed,
        was killed, or the PC lost power while recording or mixing)."""
        stamps = {
            p.name[: -len("_sys.wav")] for p in TMP_DIR.glob("*_sys.wav")
        } | {p.name[: -len("_mic.wav")] for p in TMP_DIR.glob("*_mic.wav")}
        current = self._stamp if self._threads else None
        retry_later = False
        for stamp in sorted(stamps):
            if stamp in self._saving or stamp == current:
                continue
            sys_tmp, mic_tmp, meta_path = self._tmp_paths(stamp)
            files = [p for p in (sys_tmp, mic_tmp) if p.exists()]
            try:
                newest = max(p.stat().st_mtime for p in files)
            except (OSError, ValueError):
                continue
            if time.time() - newest < RECOVERY_MIN_AGE_SECS:
                retry_later = True  # maybe another AudioSave window is recording
                continue
            if all(wav_data_bytes(p) == 0 for p in files):
                for p in (*files, meta_path):
                    p.unlink(missing_ok=True)
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
            fmt = meta.get("fmt") or ("mp3" if mp3_available() else "wav")
            env_sys = [tuple(p) for p in meta.get("env_sys") or [(0.0, 1.0)]]
            env_mic = [tuple(p) for p in meta.get("env_mic") or [(0.0, 1.0)]]
            if not self._threads:
                self.status_label.configure(
                    text=f"Recovering unsaved recording {stamp}...", fg=AMBER
                )
            self._start_save(stamp, env_sys, env_mic, fmt, recovered=True)
        if retry_later:
            self.after(int(RECOVERY_MIN_AGE_SECS * 1000) + 5000, self._recover_interrupted)

    # ---- polling / callbacks ---------------------------------------------- #
    def _poll(self) -> None:
        while True:
            try:
                msg = self._status_q.get_nowait()
            except queue.Empty:
                break
            if isinstance(msg, tuple):
                self._on_save_result(*msg)
            else:
                # plain status string from a capture thread
                if self._threads and any(t.is_alive() for t in self._threads):
                    self.status_label.configure(text=str(msg), fg=FG_MUTED)
        if self._capture_finished():
            self._finalize_recording()
        if self._closing and not self._threads and not self._saving:
            self._shutdown()
            return
        self.after(120, self._poll)

    def _on_save_result(self, kind: str, payload) -> None:
        if kind == "done":
            stamp, created, recovered = payload
            self._saving.discard(stamp)
            names = ", ".join(p.name for p in created)
            if recovered:
                self.status_label.configure(
                    text=f"Recovered an unsaved recording from last session: {names}",
                    fg=GREEN,
                )
            else:
                self.status_label.configure(text=f"Saved {names}", fg=GREEN)
            self.refresh_recordings()
        elif kind == "error":
            stamp, err, rescued = payload
            self._saving.discard(stamp)
            self.refresh_recordings()
            if rescued:
                names = ", ".join(p.name for p in rescued)
                text = (
                    f"Couldn't create the final file ({err}).\n\n"
                    f"Your audio is safe - the raw track(s) were saved as: {names}"
                )
                self.status_label.configure(text=f"Saved raw track(s): {names}", fg=AMBER)
            else:
                text = (
                    f"Couldn't save recording {stamp}: {err}\n\n"
                    f"The captured audio was kept in {TMP_DIR} and AudioSave "
                    "will try again next time it starts."
                )
                self.status_label.configure(text=f"Error: {err}", fg=RED)
            if not self._closing:
                messagebox.showwarning("AudioSave", text)

    def _set_level_sys(self, level: float) -> None:
        # Called from the capture thread: only store the raw peak (atomic
        # float). The gain is applied for display on the main thread in
        # _draw_meter, so the meters show input level x volume setting.
        self._level_sys = level

    def _set_level_mic(self, level: float) -> None:
        self._level_mic = level

    def _draw_meter(self, key: str) -> None:
        """Refresh one meter: input level x current volume (clipped at full).

        Only ever called on the main thread (from _tick or a slider move).
        """
        raw = self._level_sys if key == "sys" else self._level_mic
        gain = (
            self._gain_sys.get() / 100.0
            if key == "sys"
            else self._gain_mic.get() / 100.0
        )
        eff = min(1.0, raw * gain)
        m = self._meter_sys if key == "sys" else self._meter_mic
        m.set(eff)
        m.draw()

    def _record_gain(self, key: str, gain: float) -> None:
        """Log a volume change (seconds-since-start, gain) while recording."""
        if self._started_at is None or key not in self._active_sources:
            return
        env = self._env_sys if key == "sys" else self._env_mic
        if env and abs(env[-1][1] - gain) < 1e-9:
            return  # unchanged - skip redundant points
        env.append((time.monotonic() - self._started_at, gain))
        self._save_meta()

    # ---- recordings actions ------------------------------------------------ #
    def _selected_path(self) -> Path | None:
        sel = self.rec_list.curselection()
        if not sel or sel[0] >= len(self._rec_files):
            return None
        return self._rec_files[sel[0]]

    def _on_list_right_click(self, event) -> None:
        # Select the item under the cursor, then pop up the menu.
        try:
            idx = self.rec_list.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return  # clicked empty space
        self.rec_list.selection_clear(0, "end")
        self.rec_list.selection_set(idx)
        self.rec_list.activate(idx)
        try:
            self._rec_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._rec_menu.grab_release()

    def _rename_selected(self) -> None:
        p = self._selected_path()
        if p is None or not p.exists():
            return
        new_name = self._prompt_rename(p)
        if not new_name:
            return  # cancelled

        # Drop any audio extension the user typed, then keep the real one.
        new_path = p.with_name(strip_audio_ext(new_name) + p.suffix)
        if new_path == p:
            return  # same name (incl. case-only change) - nothing to do
        if new_path.exists():
            messagebox.showerror("AudioSave", f"{new_path.name} already exists.")
            return
        renamed = [new_path.name]
        sib_note = ""
        try:
            p.rename(new_path)
        except OSError as exc:
            messagebox.showerror("AudioSave", f"Could not rename: {exc}")
            return
        # If this track was also saved in the other format (MP3 + WAV pair),
        # rename the sibling too so the pair stays together.
        other = {".mp3": ".wav", ".wav": ".mp3"}.get(p.suffix.lower())
        if other:
            sib = p.with_suffix(other)
            if sib.exists():
                new_sib = new_path.with_suffix(other)
                if not new_sib.exists():
                    try:
                        sib.rename(new_sib)
                        renamed.append(new_sib.name)
                    except OSError:
                        sib_note = f" ({sib.name} not renamed)"
                else:
                    sib_note = f" ({new_sib.name} already exists - sibling not renamed)"
        self.refresh_recordings()
        done = f"Renamed to {renamed[0]}"
        if len(renamed) > 1:
            done += f" (and {renamed[1]})"
        self.status_label.configure(text=done + sib_note, fg=GREEN)

    def _prompt_rename(self, path: Path) -> str | None:
        """Modal rename dialog; returns the new file name (without extension)
        or None if the user cancelled."""
        dialog = tk.Toplevel(self)
        dialog.title("Rename")
        dialog.configure(bg=PANEL)
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        frame = tk.Frame(dialog, bg=PANEL)
        frame.pack(fill="both", expand=True, padx=18, pady=14)

        tk.Label(
            frame, text="Rename track:", bg=PANEL, fg=FG, font=("Segoe UI", 10)
        ).pack(anchor="w")
        entry = tk.Entry(
            frame, width=44, bg=PANEL_2, fg=FG, insertbackground=FG,
            relief="flat", highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=ACCENT, font=("Segoe UI", 10),
        )
        entry.pack(fill="x", pady=(6, 4))
        tk.Label(
            frame, text=f"Extension '.{path.suffix.lstrip('.')}' will be kept.",
            bg=PANEL, fg=FG_MUTED, font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 10))

        err = tk.Label(
            frame, text="", bg=PANEL, fg=RED, font=("Segoe UI", 9), wraplength=340
        )
        err.pack(anchor="w", pady=(0, 8))

        result: list[str] = []

        def submit(_event=None) -> None:
            name = entry.get().strip()
            msg = validate_rename_name(name)
            if msg:
                err.configure(text=msg)
                return
            result.append(name)
            dialog.destroy()

        def cancel(_event=None) -> None:
            dialog.destroy()

        buttons = tk.Frame(frame, bg=PANEL)
        buttons.pack(fill="x")
        self._mk_btn(buttons, "Cancel", cancel).pack(side="right", padx=(6, 0))
        self._mk_btn(buttons, "Rename", submit, primary=True).pack(side="right")

        entry.insert(0, path.stem)
        entry.select_range(0, "end")
        entry.icursor("end")
        entry.focus_set()
        entry.bind("<Return>", submit)
        entry.bind("<Escape>", cancel)
        dialog.bind("<Escape>", cancel)

        # Center over the main window.
        self.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dialog.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.wait_window(dialog)
        return result[0] if result else None

    def _play_selected(self) -> None:
        p = self._selected_path()
        if p and p.exists():
            try:
                os.startfile(str(p))  # Windows: open with default player
            except OSError as exc:
                messagebox.showerror("AudioSave", f"Could not play: {exc}")

    def _open_folder(self) -> None:
        try:
            os.startfile(str(RECORDINGS_DIR))
        except OSError as exc:
            messagebox.showerror("AudioSave", f"Could not open folder: {exc}")

    def _delete_selected(self) -> None:
        p = self._selected_path()
        if p and p.exists() and messagebox.askyesno("AudioSave", f"Delete {p.name}?"):
            try:
                p.unlink()
            except OSError as exc:
                messagebox.showerror("AudioSave", str(exc))
            self.refresh_recordings()

    # ---- close ------------------------------------------------------------ #
    def _on_close(self) -> None:
        """Closing never throws a recording away: an in-progress recording is
        stopped and saved, and a save in progress is finished, before the
        window goes away."""
        if self._closing:
            return
        if self._threads:
            if not messagebox.askokcancel(
                "AudioSave",
                "A recording is in progress.\n\n"
                "AudioSave will stop and save it, then close.",
            ):
                return
            self._request_stop()
        if self._threads or self._saving:
            self._closing = True
            self.status_label.configure(
                text="Saving your recording - AudioSave will close when it's done...",
                fg=AMBER,
            )
            return  # _poll calls _shutdown once everything is saved
        self._shutdown()

    def _shutdown(self) -> None:
        for t in self._threads:
            t.request_stop()
        self._engine.close()
        self.destroy()


def selftest() -> int:
    """``AudioSave.exe --selftest`` - check that this build can see the audio
    devices and the bundled ffmpeg, without opening the window. Prints a
    report and also writes it to a file (path optional, 2nd argument)."""
    from recorder import get_ffmpeg_exe

    lines = [f"AudioSave selftest ({'frozen exe' if getattr(sys, 'frozen', False) else 'source'})",
             f"recordings folder: {RECORDINGS_DIR}"]
    ok = True
    try:
        engine = AudioEngine()
        lines.append(f"speakers found: {len(engine.output_devices())}")
        lines.append(f"microphones found: {len(engine.input_devices())}")
        default = engine.default_output()
        lines.append(f"default speaker: {default['name'] if default else None}")
        ok = bool(engine.output_devices() or engine.input_devices())
        engine.close()
    except Exception as exc:  # noqa: BLE001
        lines.append(f"AUDIO ERROR: {exc}")
        ok = False
    lines.append(f"bundled ffmpeg: {get_ffmpeg_exe()}")
    lines.append(f"MP3 encoding available: {mp3_available()}")
    ok = ok and mp3_available()
    # Record 3 s and mix it, so a packaged build is proven end to end.
    try:
        import tempfile

        engine = AudioEngine()
        speaker = engine.default_output()
        mics = engine.input_devices()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            threads = []
            if speaker:
                threads.append(CaptureThread(tmp / "s.wav", "system", speaker, pa=engine.pa))
            if mics:
                threads.append(CaptureThread(tmp / "m.wav", "mic", mics[0], pa=engine.pa))
            for t in threads:
                t.start()
            time.sleep(3)
            for t in threads:
                t.stop()
            errs = [str(t.error) for t in threads if t.error]
            created = mix_and_encode(
                tmp / "s.wav" if (tmp / "s.wav").exists() else None,
                tmp / "m.wav" if (tmp / "m.wav").exists() else None,
                1.0, 1.0, tmp / "test", "mp3",
            )
            size = created[0].stat().st_size if created else 0
            lines.append(f"3 s record + mix: {size} bytes" + (f" (warnings: {errs})" if errs else ""))
            ok = ok and size > 0
        engine.close()
    except Exception as exc:  # noqa: BLE001
        lines.append(f"RECORD/MIX ERROR: {exc}")
        ok = False
    lines.append("RESULT: " + ("OK" if ok else "PROBLEMS FOUND"))
    report = "\n".join(lines)
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.cwd() / "audiosave-selftest.txt"
    try:
        out.write_text(report + "\n", encoding="utf-8")
    except OSError:
        pass
    print(report)
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:2]:
        raise SystemExit(selftest())
    App().mainloop()
