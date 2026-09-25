"""Diagnostics: list devices, run a short capture, mix, and encode smoke test."""

import time
from pathlib import Path

import pyaudiowpatch as pyaudio

from recorder import AudioEngine, CaptureThread, mix_and_encode, mp3_available

TMP = Path("recordings") / ".tmp"


def main() -> None:
    engine = AudioEngine()

    print("== Speakers (outputs) ==")
    for d in engine.output_devices():
        print(f"  [{d['index']}] {d['name']}  out_ch={d['maxOutputChannels']} rate={int(d['defaultSampleRate'])}")

    print("== Microphones (inputs) ==")
    for d in engine.input_devices():
        print(f"  [{d['index']}] {d['name']}  in_ch={d['maxInputChannels']} rate={int(d['defaultSampleRate'])}")

    print("== WASAPI loopback devices ==")
    pa = pyaudio.PyAudio()
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    wasapi_index = wasapi["index"]
    for idx in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(idx)
        if info.get("hostApi") == wasapi_index and info.get("isLoopbackDevice"):
            print(f"  [{info['index']}] {info['name']}")
    # NOTE: keep this pa instance alive and share it with all CaptureThreads -
    # constructing PyAudio() from several threads simultaneously segfaults.
    # It is terminated at the very end after all threads have stopped.

    speaker = engine.default_output()
    print(f"\nDefault output: {speaker['name'] if speaker else None}")
    mics = engine.input_devices()
    mic = mics[0] if mics else None
    engine.close()

    print("\nMP3 encoding available:", mp3_available())

    TMP.mkdir(exist_ok=True)
    stamp = "diag_smoke"
    sys_path = TMP / f"{stamp}_sys.wav"
    mic_path = TMP / f"{stamp}_mic.wav"

    threads = []
    if speaker:
        t = CaptureThread(
            sys_path, "system", speaker, pa=pa,
            on_status=lambda m: print("[status]", m),
        )
        t.start()
        threads.append(("system", t))
    if mic:
        t = CaptureThread(
            mic_path, "mic", mic, pa=pa,
            on_status=lambda m: print("[status]", m),
        )
        t.start()
        threads.append(("mic", t))

    print("\nRecording 3 seconds...")
    time.sleep(3)
    for name, t in threads:
        t.stop()
        print(f"{name}: elapsed={t.elapsed:.2f}s frames={t.total_frames} actual_rate={t.actual_rate:.0f} error={t.error}")

    if any(t.error for _, t in threads):
        print("\nERROR:", [str(t.error) for _, t in threads])
        return

    print("\nMixing with gains 1.0 / 1.0 and encoding MP3 + WAV...")
    created = mix_and_encode(
        sys_path if sys_path.exists() else None,
        mic_path if mic_path.exists() else None,
        1.0, 1.0,
        Path("recordings") / stamp,
        "both",
    )
    for p in created:
        print("  created:", p, p.stat().st_size, "bytes")

    for p in (sys_path, mic_path, Path("recordings") / f"{stamp}.wav", Path("recordings") / f"{stamp}.mp3"):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
    print("cleanup done")
    pa.terminate()
    print("pa terminated")


if __name__ == "__main__":
    main()
