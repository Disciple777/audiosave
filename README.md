# AudioSave 🎙️

Record **everything** your computer hears — system audio (what plays through your speakers) **and** your microphone — mixed into one **MP3** (or WAV) file.

Built on Windows WASAPI loopback via [PyAudioWPatch](https://pypi.org/project/PyAudioWPatch/).

## Features

- 🎛️ Capture **system audio** (game audio, music, streams, calls) and/or **microphone**
- 🎚️ Pick which speaker and mic to record from (drop-downs)
- 🎚️ **Per-source volume (0–300%) with live automation** — boost quiet system audio, and slider moves made *mid-recording* are baked into the track at the exact moment you make them
- 📊 **Two live level meters** (desktop + mic) showing **input × volume**, so boosting is reflected in real time (full red = clipping)
- 🎵 Output **MP3** (default), **WAV**, or both
- 💾 Recordings library: play, open folder, delete, **rename** (Rename button, F2, or right-click)
- 🛟 **Every recording is saved** — even if the app crashes, is closed mid-recording, or the take runs for hours (see *Reliability*)
- ⌨️ Space bar starts/stops recording

## Requirements

- Windows 10/11
- Python 3.9+

## Setup

```bash
pip install -r requirements.txt
```

> No system ffmpeg needed — a static ffmpeg binary is bundled via `imageio-ffmpeg` for high-quality mixing and MP3 encoding.

## Usage

```bash
python app.py
```

1. Pick your **speaker** (the "System audio" dropdown) and **microphone**

2. Toggle which sources to include

3. Set the **Desktop** / **Mic** volume sliders (system audio can be quiet — crank it to 200–300% if needed)

4. Choose **Save as**: MP3 (recommended), WAV, or MP3 + WAV

5. Hit **Record** (or press Space). Hit **Stop** when done.

6. Your recording appears in the right-hand list — double-click to play. To rename it, select it and click **✎ Rename** (or press **F2**, or right-click → **Rename…**) — no need to touch the file explorer.

   > Renaming keeps the file's extension automatically (type a plain name like `My mix` and `My mix.mp3` is created); if the track was saved as an MP3 **and** WAV pair, both files are renamed together. The app blocks names Windows can't use (illegal characters like `<>:"/\\|?*`, reserved names like `CON`/`NUL`, empty names, trailing dots/spaces).

Files are saved to `recordings/` as `rec_YYYY-MM-DD_HH-MM-SS.mp3`.

> The volume sliders apply **live**: each move is timestamped while you record, and the exported file follows your volume changes through the track (e.g. crank Desktop to 200% halfway through and the first half stays at the old level, the second half at the new one).

## Reliability

AudioSave is built so a recording can't silently disappear:

- **Crash-safe capture** — while you record, each source is written to `recordings/.tmp/` as a WAV whose header is refreshed every 2 s, so the file on disk is always playable. Your format choice and volume automation are saved alongside it.
- **Automatic recovery** — if AudioSave crashed, was killed, or the PC lost power, the unsaved recording is finished (mixed + encoded, with your volume moves) the next time you start the app. Temp files are never just deleted.
- **Closing is safe** — closing the window mid-recording stops and saves the take first; closing while a take is still being saved waits for it to finish.
- **Long recordings** — mixing streams through the audio in 10 s blocks, so memory use stays flat (~20 MB) whether the take is 5 minutes or 5 hours. (It used to load everything into RAM, and long takes could run out of memory.) Capture files may grow past 4 GB.
- **Fallback** — if the final MP3/WAV can't be created for any reason, the raw tracks are moved into `recordings/` as `rec_…_desktop.wav` / `rec_…_mic.wav` and you get a warning, instead of losing the audio.
- **Hung devices** — if an audio device stops responding when you press Stop, AudioSave saves what it captured after 8 s instead of hanging.

## Diagnostics

```bash
python diag.py
```

Prints every speaker/mic/loopback device, runs a 3-second capture of both sources, mixes, and encodes an MP3 + WAV to verify the whole pipeline.

## How it works

- Windows exposes each playback device as a "loopback" capture device (WASAPI loopback). AudioSave records that endpoint — the exact sound your speakers output — plus a normal microphone stream.
- Each source is captured by its **own thread**, paced by the wall clock so the recording duration always matches real time (no speed-up, no drop-outs). Silence is padded in when nothing plays through the speakers.
- After you hit Stop, the tracks are **mixed offline**: each source is resampled to 48 kHz (bundled ffmpeg), its **volume automation envelope**(every slider move, timestamped while you recorded) is applied, the tracks are summed, and the result is encoded to MP3/WAV.

The to-do list for this project is GOALSTRUCK.md, synced from Goalstruck.
Tick items you finish and run `node goalstruck.mjs push`. Never edit the
<!--gs:NNN--> comments.