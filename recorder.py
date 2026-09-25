"""AudioSave - core recording engine.

Captures system audio (WASAPI loopback, i.e. what plays through your
speakers) and/or microphone input on Windows. Each selected source is
recorded by its own thread into a separate WAV file; the threads are paced
by the wall clock so the recorded duration always matches the real elapsed
time (no speed-up, no drop-outs). After recording stops, the tracks are
mixed offline with per-track **volume automation** (0-300%, and the gain can
change through the recording - slider moves made mid-recording are baked
into the output at the exact moment they happened) and encoded to MP3
and/or WAV using a bundled ffmpeg (via imageio-ffmpeg), with a pure-Python
fallback.

Durability: the capture WAVs are kept valid on disk while recording (header
refreshed every few seconds), and mixing streams through the tracks in
blocks, so neither a crash nor a very long recording can lose the audio.
"""

from __future__ import annotations

import os
import struct
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Callable, Iterator, Optional

import numpy as np
import pyaudiowpatch as pyaudio

CHUNK = 1024
SAMPLE_WIDTH = 2  # int16
MIX_RATE = 48000  # output sample rate for the mixed file
MIX_BLOCK = MIX_RATE * 10  # frames mixed per block (10 s) - bounds memory use
MP3_BITRATE = "192k"
HEADER_SYNC_SECS = 2.0  # how often a capture file's WAV header is refreshed

# Don't flash a console window for every ffmpeg call when run via pythonw.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# PortAudio is a process-wide singleton; opening streams from multiple
# threads at once can race. Serialize stream creation with this lock.
_OPEN_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# device discovery
# --------------------------------------------------------------------------

def find_loopback(pa: pyaudio.PyAudio, speaker: dict) -> Optional[dict]:
    """Find the WASAPI loopback device that mirrors a given speaker.

    Loopback device names contain the speaker name, e.g.
    "Speakers (Realtek(R) Audio) [Loopback]".
    Falls back to the default WASAPI loopback device if no name match.
    """
    name = speaker.get("name", "")
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        wasapi_index = wasapi["index"]
        for idx in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(idx)
            if (
                info.get("hostApi") == wasapi_index
                and info.get("isLoopbackDevice", False)
                and name
                and name in info.get("name", "")
            ):
                return info
    except Exception:
        pass
    # Fallback: the system default loopback endpoint - but ONLY when the
    # requested speaker is the default output device. Otherwise recording the
    # default loopback could silently capture a different speaker than the one
    # the user selected.
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        if speaker.get("index") == wasapi["defaultOutputDevice"]:
            return pa.get_default_wasapi_loopback()
    except Exception:
        pass
    return None


def _as_stereo(data: np.ndarray, channels: int) -> np.ndarray:
    """Ensure the array has 2 columns (stereo)."""
    if data.ndim == 1:
        data = data.reshape(-1, 1)
    if channels == 1:
        return np.repeat(data, 2, axis=1)
    if data.shape[1] > 2:
        return data[:, :2]
    return data


class AudioEngine:
    """Device discovery helpers (speakers, microphones, loopback)."""

    def __init__(self) -> None:
        self._pa = pyaudio.PyAudio()

    def close(self) -> None:
        try:
            self._pa.terminate()
        except Exception:
            pass

    def _wasapi_devices(self) -> list[dict]:
        wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        wasapi_index = wasapi["index"]
        return [
            self._pa.get_device_info_by_index(i)
            for i in range(self._pa.get_device_count())
            if self._pa.get_device_info_by_index(i).get("hostApi") == wasapi_index
        ]

    def output_devices(self) -> list[dict]:
        """Speaker (output) devices, loopback devices excluded."""
        return [
            d
            for d in self._wasapi_devices()
            if d.get("maxOutputChannels", 0) > 0 and not d.get("isLoopbackDevice", False)
        ]

    def input_devices(self) -> list[dict]:
        """Microphone (input) devices, loopback devices excluded."""
        return [
            d
            for d in self._wasapi_devices()
            if d.get("maxInputChannels", 0) > 0 and not d.get("isLoopbackDevice", False)
        ]

    def default_output(self) -> Optional[dict]:
        try:
            wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            return self._pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        except Exception:
            return None

    @property
    def pa(self) -> pyaudio.PyAudio:
        """The shared PyAudio instance (one per process - see CaptureThread)."""
        return self._pa


# --------------------------------------------------------------------------
# per-source capture
# --------------------------------------------------------------------------

_WAV_MAX_DATA = 0xFFFFFFFF - 36  # largest data size a WAV header can hold


def _wav_header(rate: int, channels: int, data_bytes: int) -> bytes:
    """44-byte header of a 16-bit PCM WAV. Sizes past 4 GB are clamped (the
    readers in this module ignore the length and read to end of file)."""
    block = channels * SAMPLE_WIDTH
    data = min(int(data_bytes), _WAV_MAX_DATA)
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data, b"WAVE",
        b"fmt ", 16, 1, channels, int(rate), int(rate) * block, block, 16,
        b"data", data,
    )


class WavWriter:
    """Minimal 16-bit PCM WAV writer built for long, crash-prone recordings.

    Unlike the stdlib ``wave`` module it never raises past 4 GB (that would
    abort a recording after ~6 hours), and ``sync()`` rewrites the header and
    flushes to the OS, so if the app dies mid-recording the file on disk is
    still a valid WAV with everything captured up to the last sync.
    """

    def __init__(self, path: str | Path, rate: int, channels: int = 2) -> None:
        self.rate = int(rate)
        self.channels = channels
        self.data_bytes = 0
        self._f = open(path, "wb")
        self._f.write(_wav_header(self.rate, channels, 0))

    def write(self, data: bytes) -> None:
        self._f.write(data)
        self.data_bytes += len(data)

    def sync(self) -> None:
        pos = self._f.tell()
        self._f.seek(0)
        self._f.write(_wav_header(self.rate, self.channels, self.data_bytes))
        self._f.seek(pos)
        self._f.flush()

    def close(self) -> None:
        if not self._f.closed:
            try:
                self.sync()
            finally:
                self._f.close()


def _parse_wav(path: Path) -> tuple[int, int, int]:
    """Return (channels, sample_rate, data_offset) of a 16-bit PCM WAV.

    The data chunk's declared length is deliberately ignored: capture files
    from a crashed session (or longer than 4 GB) have a stale/clamped length,
    so callers read from ``data_offset`` to end of file instead.
    """
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise RuntimeError(f"{Path(path).name} is not a WAV file.")
        channels = rate = 0
        while True:
            chunk = f.read(8)
            if len(chunk) < 8:
                raise RuntimeError(f"{Path(path).name} has no audio data.")
            cid, size = struct.unpack("<4sI", chunk)
            if cid == b"fmt ":
                fmt = f.read(size)
                _, channels, rate, _, _, bits = struct.unpack("<HHIIHH", fmt[:16])
                if bits != 16:
                    raise RuntimeError(f"{Path(path).name}: only 16-bit WAV is supported.")
                if size % 2:
                    f.read(1)
            elif cid == b"data":
                if not channels:
                    raise RuntimeError(f"{Path(path).name}: data before format chunk.")
                return channels, rate, f.tell()
            else:
                f.seek(size + (size % 2), 1)


def wav_data_bytes(path: str | Path) -> int:
    """Bytes of audio actually on disk in a WAV (0 if unreadable)."""
    try:
        _, _, offset = _parse_wav(Path(path))
        return max(0, Path(path).stat().st_size - offset)
    except Exception:
        return 0


def repair_wav_header(path: str | Path) -> None:
    """Make a WAV's size fields match what is really on disk (e.g. a capture
    file left behind by a crash), so any media player reads all of it."""
    path = Path(path)
    channels, rate, offset = _parse_wav(path)
    data = path.stat().st_size - offset
    data -= data % (channels * SAMPLE_WIDTH)
    if offset == 44:
        with open(path, "r+b") as f:
            f.write(_wav_header(rate, channels, data))


def _patch_wav_rate(path: Path, rate: int) -> None:
    """Rewrite the sample-rate and byte-rate fields (bytes 24-31) of a
    44-byte-header PCM WAV."""
    with open(path, "r+b") as f:
        f.seek(22)
        channels = struct.unpack("<H", f.read(2))[0]
        f.write(struct.pack("<II", int(rate), int(rate) * channels * SAMPLE_WIDTH))


class CaptureThread(threading.Thread):
    """Records ONE source (system loopback or microphone) to its own WAV.

    The loop is paced by the wall clock: it never writes more frames than
    the real elapsed time implies, so the file's duration always matches the
    wall-clock duration - this is what fixes the "sped up / choppy" audio.
    When a loopback stream stalls (nothing playing through the speakers),
    silence is padded in so the timeline stays continuous.

    IMPORTANT: all CaptureThreads must share the same pyaudio.PyAudio()
    instance (passed in as ``pa``). PortAudio is a process-wide singleton;
    constructing PyAudio() from several threads simultaneously segfaults.
    The caller owns the instance and must terminate() it only after all
    threads have stopped.
    """

    def __init__(
        self,
        output_path: str | Path,
        kind: str,  # 'system' | 'mic'
        device: dict,
        pa: pyaudio.PyAudio,
        on_level: Optional[Callable[[float], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(daemon=True)
        self.output_path = Path(output_path)
        self.kind = kind
        self.device = device
        self._pa = pa
        self.on_level = on_level
        self.on_status = on_status
        # NOTE: must not be named _stop - threading.Thread has an internal
        # _stop() method that join() relies on.
        self._stop_event = threading.Event()
        self.error: Optional[Exception] = None
        self.elapsed = 0.0
        self.total_frames = 0
        self.actual_rate = 0.0

    def request_stop(self) -> None:
        """Ask the thread to finish without waiting for it (UI-safe)."""
        self._stop_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=6)

    def _emit_status(self, msg: str) -> None:
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                pass

    def _emit_level(self, peak: float) -> None:
        if self.on_level:
            try:
                self.on_level(peak)
            except Exception:
                pass

    def run(self) -> None:
        pa = self._pa
        st = None
        wf = None
        rate = 0
        try:
            if self.kind == "system":
                loopback = find_loopback(pa, self.device)
                if loopback is None:
                    raise RuntimeError(
                        f"Could not find a loopback capture device for "
                        f"'{self.device.get('name', '?')}'. Try another output device."
                    )
                dev = loopback
            else:
                dev = self.device
            ch = max(1, int(dev.get("maxInputChannels") or 2))
            rate = int(dev["defaultSampleRate"])
            with _OPEN_LOCK:
                st = pa.open(
                    format=pyaudio.paInt16,
                    channels=ch,
                    rate=rate,
                    input=True,
                    input_device_index=dev["index"],
                    frames_per_buffer=CHUNK,
                )
            wf = WavWriter(self.output_path, rate, 2)

            self._emit_status(f"Recording {self.kind}...")
            start = time.monotonic()
            last_sync = start
            written = 0
            while not self._stop_event.is_set():
                now = time.monotonic()
                expected = int((now - start) * rate)  # frames that SHOULD exist now
                if expected <= written:
                    # caught up to real time - wait a moment for new frames
                    if self._stop_event.wait(0.005):
                        break
                    continue
                need = min(expected - written, CHUNK * 2)

                if self.kind == "system":
                    # Loopback stalls when nothing is playing and read() would
                    # block forever - poll what is available instead.
                    try:
                        avail = st.get_read_available()
                    except Exception:
                        avail = 0
                    if avail <= 0:
                        arr = np.zeros((need, ch), dtype=np.int16)
                        peak = 0.0
                    else:
                        n = min(avail, need)
                        try:
                            raw = st.read(n, exception_on_overflow=False)
                        except Exception:
                            raw = b""
                        if raw:
                            arr = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch)
                            peak = float(np.max(np.abs(arr))) / 32768.0
                            if len(arr) < need:
                                arr = np.vstack(
                                    [arr, np.zeros((need - len(arr), ch), dtype=np.int16)]
                                )
                        else:
                            arr = np.zeros((need, ch), dtype=np.int16)
                            peak = 0.0
                else:
                    # Microphone streams always deliver in real time.
                    try:
                        raw = st.read(need, exception_on_overflow=False)
                    except Exception:
                        raw = b""
                    if raw:
                        arr = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch)
                        peak = float(np.max(np.abs(arr))) / 32768.0
                    else:
                        arr = np.zeros((need, ch), dtype=np.int16)
                        peak = 0.0

                wf.write(_as_stereo(arr, ch).tobytes())
                written += len(arr)
                # Kept current as we go (not just at the end) so that if this
                # thread dies mid-recording, what it captured is still mixed.
                self.total_frames = written
                self._emit_level(min(1.0, peak))
                if now - last_sync >= HEADER_SYNC_SECS:
                    wf.sync()  # file on disk stays a valid WAV if we crash
                    last_sync = now

            self.elapsed = time.monotonic() - start
            if self.elapsed > 0:
                self.actual_rate = written / self.elapsed
        except Exception as exc:  # noqa: BLE001 - report any capture error
            self.error = exc
            self._emit_status(f"Error: {exc}")
        finally:
            if st is not None:
                try:
                    st.stop_stream()
                    st.close()
                except Exception:
                    pass
            try:
                if wf is not None:
                    wf.close()
            except Exception:
                pass
            # NOTE: do NOT call pa.terminate() here - the PyAudio instance is
            # shared with the other capture threads and the caller owns it.

        # If the device delivered at a slightly different rate than reported
        # (e.g. a 44.1 kHz mic labelled 48 kHz), patch the header so the file
        # plays back at the correct speed and duration. (WASAPI shared-mode
        # loopback normally delivers at the exact rate we opened the stream
        # with, so in practice this is a no-op safeguard.)
        if (
            self.error is None
            and self.actual_rate > 0
            and self.total_frames > 0
            and rate > 0
            and abs(self.actual_rate - rate) / rate > 0.005
        ):
            try:
                _patch_wav_rate(self.output_path, int(round(self.actual_rate)))
            except Exception:
                pass


# --------------------------------------------------------------------------
# mixing & encoding
# --------------------------------------------------------------------------

def get_ffmpeg_exe() -> Optional[str]:
    """Path to the bundled ffmpeg binary (imageio-ffmpeg), or None."""
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        return exe if Path(exe).exists() else None
    except Exception:
        return None


def mp3_available() -> bool:
    return get_ffmpeg_exe() is not None


def _ffmpeg(ffmpeg: str, args: list[str], **kw) -> tuple[subprocess.Popen, object]:
    """Start ffmpeg with stderr going to a temp file (a stderr pipe nobody
    reads can fill up and deadlock a long-running stream)."""
    err = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(
            [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", *args],
            stderr=err, creationflags=_NO_WINDOW, **kw,
        )
    except Exception as exc:  # noqa: BLE001
        err.close()
        raise RuntimeError(f"Could not run ffmpeg: {exc}") from exc
    return proc, err


def _ffmpeg_error(err, what: str) -> RuntimeError:
    err.seek(0)
    msg = err.read().decode("utf-8", errors="replace").strip()
    return RuntimeError(f"ffmpeg {what} failed: {msg[-400:] or 'unknown error'}")


def _iter_mix_blocks(
    ffmpeg: str,
    sources: list[tuple[Path, list[tuple[float, float]]]],
    rate: int,
) -> Iterator[np.ndarray]:
    """Stream the mix: yield float32 stereo blocks (<= MIX_BLOCK frames) of
    all sources resampled to ``rate`` by ffmpeg, each with its gain envelope
    applied, summed. Memory use is a few blocks regardless of how long the
    recording is (the old all-in-RAM mix needed ~4 GB per track per 3 hours).
    """
    decoders = []
    try:
        for path, env in sources:
            proc, err = _ffmpeg(
                ffmpeg,
                # -ignore_length: read to end of file even if the WAV header's
                # size is stale (crash-recovered file) or clamped (> 4 GB).
                ["-ignore_length", "1", "-i", str(path),
                 "-f", "s16le", "-ac", "2", "-ar", str(rate), "-"],
                stdout=subprocess.PIPE,
            )
            decoders.append((proc, err, env))
        offset = 0
        block_bytes = MIX_BLOCK * 2 * SAMPLE_WIDTH
        while True:
            mixed: Optional[np.ndarray] = None
            for proc, _err, env in decoders:
                raw = proc.stdout.read(block_bytes)  # blocks until full or EOF
                raw = raw[: len(raw) - len(raw) % (2 * SAMPLE_WIDTH)]
                if not raw:
                    continue
                d = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2)
                d = _apply_gain_envelope(d.astype(np.float32) / 32768.0, rate, env, offset)
                if mixed is None:
                    mixed = d
                elif len(d) > len(mixed):
                    d[: len(mixed)] += mixed
                    mixed = d
                else:
                    mixed[: len(d)] += d
            if mixed is None:
                break
            yield mixed
            offset += len(mixed)
        for proc, err, _env in decoders:
            if proc.wait() != 0:
                raise _ffmpeg_error(err, "decode")
    finally:
        for proc, err, _env in decoders:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            err.close()


def _to_int16(block: np.ndarray, scale: float) -> bytes:
    return (np.clip(block * scale, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


def _mix_ffmpeg_streaming(
    ffmpeg: str,
    sources: list[tuple[Path, list[tuple[float, float]]]],
    out_stem: Path,
    fmt: str,
) -> list[Path]:
    """Two streaming passes: the first finds the mix's peak (so it can be
    scaled down if it would clip, same as before), the second writes the
    WAV and/or pipes PCM into ffmpeg's MP3 encoder. Outputs are written to
    ``*.part`` files and only renamed into place once complete, so a failed
    or interrupted mix never leaves a truncated file that looks finished."""
    peak = 0.0
    frames = 0
    for block in _iter_mix_blocks(ffmpeg, sources, MIX_RATE):
        peak = max(peak, float(np.max(np.abs(block))))
        frames += len(block)
    if frames == 0:
        raise RuntimeError("No audio was recorded.")
    scale = 0.98 / peak if peak > 1.0 else 1.0  # gentle safety against clipping

    finals: list[Path] = []
    wav_part = mp3_part = None
    wav = enc = enc_err = None
    try:
        if fmt in ("wav", "both"):
            finals.append(out_stem.with_suffix(".wav"))
            wav_part = out_stem.with_suffix(".wav.part")
            wav = WavWriter(wav_part, MIX_RATE, 2)
        if fmt in ("mp3", "both"):
            finals.append(out_stem.with_suffix(".mp3"))
            mp3_part = out_stem.with_suffix(".mp3.part")
            enc, enc_err = _ffmpeg(
                ffmpeg,
                ["-y", "-f", "s16le", "-ar", str(MIX_RATE), "-ac", "2", "-i", "-",
                 "-c:a", "libmp3lame", "-b:a", MP3_BITRATE, "-f", "mp3", str(mp3_part)],
                stdin=subprocess.PIPE,
            )
        for block in _iter_mix_blocks(ffmpeg, sources, MIX_RATE):
            pcm = _to_int16(block, scale)
            if wav is not None:
                wav.write(pcm)
            if enc is not None:
                try:
                    enc.stdin.write(pcm)
                except OSError:
                    enc.wait()
                    raise _ffmpeg_error(enc_err, "MP3 encode") from None
        if wav is not None:
            wav.close()
        if enc is not None:
            enc.stdin.close()
            if enc.wait() != 0:
                raise _ffmpeg_error(enc_err, "MP3 encode")
        if wav_part is not None:
            os.replace(wav_part, out_stem.with_suffix(".wav"))
        if mp3_part is not None:
            os.replace(mp3_part, out_stem.with_suffix(".mp3"))
        return finals
    finally:
        if wav is not None:
            wav.close()
        if enc is not None:
            if enc.poll() is None:
                enc.kill()
                enc.wait()
            enc_err.close()
        for part in (wav_part, mp3_part):
            if part is not None:
                try:
                    part.unlink(missing_ok=True)
                except OSError:
                    pass


def _write_wav_pcm(pcm: np.ndarray, rate: int, path: Path) -> None:
    """Write int16 stereo PCM to a WAV file."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setframerate(rate)
        w.setsampwidth(SAMPLE_WIDTH)
        w.writeframes(pcm.tobytes())


def _mix_pcm(
    parts: list[np.ndarray],
    envs: list[list[tuple[float, float]]],
    rate: int,
) -> np.ndarray:
    """Sum resampled float tracks, each with its own gain envelope applied,
    and return int16 PCM. If the sum would clip, the whole mix is scaled down
    gently so relative levels (and the envelope) are preserved."""
    n = max(len(d) for d in parts)
    mixed = np.zeros((n, 2), dtype=np.float32)
    for d, env in zip(parts, envs):
        g = _apply_gain_envelope(d, rate, env)
        mixed[: len(g)] += g
    peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
    if peak > 1.0:
        mixed *= 0.98 / peak  # gentle safety against clipping
    return (np.clip(mixed, -1.0, 1.0) * 32767.0).astype(np.int16)


def _apply_gain_envelope(
    data: np.ndarray, rate: int, env: list[tuple[float, float]], start: int = 0
) -> np.ndarray:
    """Multiply a float stereo array by a time-varying gain (numpy path).

    ``env`` is a list of (seconds, gain) points; the most recent point's gain
    is held until the next one arrives, so a slider change made at second T
    applies from that moment on. ``data`` is float32 in [-1, 1]; ``start`` is
    the frame index of its first sample within the whole track (for blocks).
    """
    if len(data) == 0 or not env:
        return data
    if len(env) == 1:
        return data * float(env[0][1])
    pts = sorted(env, key=lambda p: p[0])
    times = np.array([p[0] for p in pts], dtype=np.float64)
    gains = np.array([p[1] for p in pts], dtype=np.float64)
    t = np.arange(start, start + len(data), dtype=np.float64) / float(rate)
    idx = np.clip(np.searchsorted(times, t, side="right") - 1, 0, len(pts) - 1)
    return data * gains[idx].astype(np.float32)[:, None]


def _norm_env(env) -> list[tuple[float, float]]:
    """Normalize a gain envelope: None / float / list of (t, gain) points."""
    if env is None:
        return [(0.0, 1.0)]
    if isinstance(env, (int, float)):
        return [(0.0, float(env))]
    pts = [(float(t), float(g)) for t, g in env]
    return pts if pts else [(0.0, 1.0)]


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Read a WAV as float32 stereo in [-1, 1], plus its sample rate. Reads
    to end of file, so crash-recovered / > 4 GB capture files work too."""
    ch, rate, offset = _parse_wav(path)
    data = np.fromfile(str(path), dtype=np.int16, offset=offset)
    data = data[: len(data) - len(data) % ch]
    arr = data.reshape(-1, ch) if ch > 1 else data.reshape(-1, 1)
    return _as_stereo(arr, ch).astype(np.float32) / 32768.0, rate


def _resample_linear(data: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear-interpolation resample of a 2-D float array (fallback only)."""
    if src_rate == dst_rate or len(data) == 0:
        return data.astype(np.float32)
    n_out = max(1, int(round(len(data) * dst_rate / src_rate)))
    x_src = np.linspace(0.0, 1.0, len(data), endpoint=False)
    x_dst = np.linspace(0.0, 1.0, n_out, endpoint=False)
    cols = [
        np.interp(x_dst, x_src, data[:, c].astype(np.float64)).astype(np.float32)
        for c in range(data.shape[1])
    ]
    return np.column_stack(cols)


def _encode_mp3_lameenc(pcm: np.ndarray, rate: int, out_path: Path) -> None:
    """Pure-Python MP3 encoding fallback (no ffmpeg required)."""
    try:
        import lameenc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "MP3 encoding needs the bundled ffmpeg (imageio-ffmpeg) or lameenc."
        ) from exc
    enc = lameenc.Encoder()
    enc.set_bit_rate(int(MP3_BITRATE.rstrip("k")))
    enc.set_in_sample_rate(rate)
    enc.set_channels(2)
    enc.set_quality(2)
    pcm = np.ascontiguousarray(pcm, dtype=np.int16)  # guard against float input
    out_path.write_bytes(enc.encode(pcm.tobytes()) + enc.flush())


def _mix_numpy(
    system_wav: Optional[Path],
    mic_wav: Optional[Path],
    sys_env,
    mic_env,
    out_stem: Path,
    fmt: str,
) -> list[Path]:
    """Fallback mixer (used only when the bundled ffmpeg is unavailable)."""
    raw: list[tuple[np.ndarray, int, list[tuple[float, float]]]] = []
    if system_wav is not None and system_wav.exists():
        raw.append((*_read_wav(system_wav), _norm_env(sys_env)))
    if mic_wav is not None and mic_wav.exists():
        raw.append((*_read_wav(mic_wav), _norm_env(mic_env)))
    if not raw:
        raise RuntimeError("No audio was recorded.")

    rate = max(r for _, r, _ in raw)
    parts = [_resample_linear(d, r, rate) for d, r, _ in raw]
    envs = [env for _, _, env in raw]
    pcm = _mix_pcm(parts, envs, rate)

    created: list[Path] = []
    if fmt in ("wav", "both"):
        p = out_stem.with_suffix(".wav")
        _write_wav_pcm(pcm, rate, p)
        created.append(p)
    if fmt in ("mp3", "both"):
        p = out_stem.with_suffix(".mp3")
        _encode_mp3_lameenc(pcm, rate, p)
        created.append(p)
    return created


def mix_and_encode(
    system_wav: Optional[Path],
    mic_wav: Optional[Path],
    sys_env,
    mic_env,
    out_stem: Path,
    fmt: str,  # 'mp3' | 'wav' | 'both'
) -> list[Path]:
    """Mix the captured tracks with per-track gain envelopes and encode to the
    requested format(s). ``sys_env``/``mic_env`` are either a constant gain
    (a float), or a list of (seconds, gain) points describing how the volume
    changed during the recording - each source's gain follows its own
    envelope, so slider moves made mid-recording are baked into the output at
    the exact moment they happened. Returns the list of files created.

    The bundled ffmpeg is used for high-quality resampling to MIX_RATE and
    for MP3 encoding; gain envelopes + mixing always run in numpy (exact,
    one code path), streamed block by block so memory use doesn't grow with
    the recording's length. If ffmpeg is unavailable, everything runs in
    numpy/lameenc.
    """
    out_stem = Path(out_stem)
    sources: list[tuple[Path, list[tuple[float, float]]]] = []
    if system_wav is not None and Path(system_wav).exists():
        sources.append((Path(system_wav), _norm_env(sys_env)))
    if mic_wav is not None and Path(mic_wav).exists():
        sources.append((Path(mic_wav), _norm_env(mic_env)))
    if not sources:
        raise RuntimeError("No audio was recorded.")

    ffmpeg = get_ffmpeg_exe()
    if ffmpeg is None:
        return _mix_numpy(system_wav, mic_wav, sys_env, mic_env, out_stem, fmt)

    return _mix_ffmpeg_streaming(ffmpeg, sources, out_stem, fmt)
