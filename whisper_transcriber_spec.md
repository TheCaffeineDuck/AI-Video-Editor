# Whisper Transcriber — Application Specification

**Version:** 1.0
**Type:** Cross-platform desktop application
**Target platforms:** Windows 10+, macOS 11+, Linux (x86_64)

---

## 1. Overview

Whisper Transcriber is a self-contained desktop application that converts video and audio files into text transcripts using OpenAI's Whisper speech-recognition model. The application runs entirely offline after initial model download — no API keys, no cloud services, no recurring costs. The end user interacts with a single window: drop in a media file, choose a model size, click a button, and receive a transcript and subtitle file.

### 1.1 Goals

- One-click launch on all three major desktop OSes.
- No Python, ffmpeg, or any developer tooling required for the end user.
- Drag-and-drop file input.
- Output as plain text (`.txt`), SubRip subtitles (`.srt`), and WebVTT (`.vtt`).
- Bundled binaries, bundled ffmpeg, automatic model download on first run.

### 1.2 Non-goals

- Real-time transcription of live audio streams.
- Speaker diarization (who-spoke-when).
- Cloud sync, accounts, or telemetry.
- Editing transcripts inside the app (user can edit the output file in any text editor).

---

## 2. Tech Stack

### 2.1 Summary

| Layer | Technology | Reason |
|---|---|---|
| Language | Python 3.11 | Whisper's reference implementation is Python; large ML ecosystem. |
| UI framework | CustomTkinter 5.2+ | Built on stdlib Tkinter, modern dark/light themes, no Qt licensing, small footprint. |
| Drag-and-drop | tkinterdnd2 | Adds native OS drag-drop to Tkinter. |
| Transcription engine | faster-whisper 1.0+ | 4× faster than reference Whisper, lower memory, identical accuracy, no PyTorch dependency. |
| Audio decoding | bundled ffmpeg binary | Whisper requires 16 kHz mono PCM; ffmpeg handles every container/codec. |
| Async work | `threading` + `queue` | Keeps the UI responsive during long transcriptions. |
| Packaging | PyInstaller 6+ (one-folder mode) | Produces a standalone bundle including Python interpreter and all dependencies. |
| Installer (Windows) | Inno Setup 6 | Free, scriptable, signed-installer support. |
| Installer (macOS) | `create-dmg` + codesign + notarization | Standard mac distribution. |
| Installer (Linux) | AppImage (linuxdeploy) | Single executable, runs on any modern distro. |

### 2.2 Dependencies (Python)

```
customtkinter==5.2.2
tkinterdnd2==0.4.2
faster-whisper==1.0.3
av==12.0.0           # PyAV, used as ffmpeg fallback for metadata
huggingface-hub==0.24.0  # model downloads
```

### 2.3 Bundled binaries

- `ffmpeg` static build (~30 MB per platform) shipped in `resources/bin/`.
- CTranslate2 shared library (comes with `faster-whisper`).

### 2.4 Disk footprint

- Base installer: ~250 MB (Python + libs + ffmpeg).
- Plus model files, downloaded on demand: tiny 75 MB, base 145 MB, small 484 MB, medium 1.5 GB, large-v3 3.0 GB.

---

## 3. Architecture

```
┌──────────────────────────────────────────────────┐
│                   main.py                        │
│  (entry point — initializes Tk root + app)       │
└───────────────┬──────────────────────────────────┘
                │
   ┌────────────┴────────────┐
   │                         │
┌──▼──────────────┐  ┌───────▼────────────┐
│   ui/           │  │   core/            │
│  - app.py       │  │  - transcriber.py  │
│  - components/  │  │  - audio.py        │
│  - theme.py     │  │  - exporters.py    │
└──┬──────────────┘  │  - models.py       │
   │                 └───────┬────────────┘
   │                         │
   │   queue.Queue (events)  │
   └─────────◄───────────────┘
```

### 3.1 Module responsibilities

- **`ui/app.py`** — main window, state machine, event routing.
- **`ui/components/`** — reusable widgets: drop zone, model picker, progress card, result card.
- **`core/transcriber.py`** — wraps `faster-whisper`, runs in a worker thread, emits progress events to a `queue.Queue`.
- **`core/audio.py`** — invokes bundled ffmpeg to extract 16 kHz mono WAV from any input.
- **`core/exporters.py`** — writes `.txt`, `.srt`, `.vtt` from segment list.
- **`core/models.py`** — model registry, download status, cache path resolution.

### 3.2 Threading model

- Main thread: Tk event loop.
- Worker thread: spawned per transcription job; reads from input file, calls Whisper, pushes progress events.
- UI polls the event queue every 100 ms via `root.after()` and updates widgets.

---

## 4. UX / UI Design

### 4.1 Window

- Title: **Whisper Transcriber**
- Default size: 760 × 560 px
- Minimum size: 640 × 480 px
- Resizable: yes
- Theme: follows OS (dark / light), accent color `#3B82F6` (blue-500).

### 4.2 Layout (idle state)

```
┌─────────────────────────────────────────────────────────┐
│  Whisper Transcriber                          ⚙  ─  □  ✕ │
├─────────────────────────────────────────────────────────┤
│                                                         │
│   ┌───────────────────────────────────────────────┐     │
│   │                                               │     │
│   │             📁  Drop a file here              │     │
│   │                                               │     │
│   │            or click to browse                 │     │
│   │                                               │     │
│   │       MP4 · MOV · MKV · MP3 · WAV · M4A       │     │
│   │                                               │     │
│   └───────────────────────────────────────────────┘     │
│                                                         │
│   Model:    ◉ base   ○ small   ○ medium   ○ large       │
│   Language: [ Auto-detect ▾ ]                           │
│   Output:   ☑ Text (.txt)   ☑ Subtitles (.srt)   ☐ VTT  │
│                                                         │
│   ┌─────────────────────────────────────────────────┐   │
│   │              [ Transcribe ]                     │   │
│   └─────────────────────────────────────────────────┘   │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### 4.3 States and transitions

| State | Trigger | UI changes |
|---|---|---|
| **Idle** | App launch | Drop zone visible, Transcribe button disabled. |
| **File loaded** | User drops or selects a file | Drop zone shows filename, duration, size; Transcribe enabled. |
| **Model downloading** | First use of a model | Progress card shows "Downloading base model (145 MB)…" with bar. |
| **Transcribing** | Click Transcribe | Inputs disabled; progress card shows percent + elapsed time + estimated remaining. |
| **Complete** | Worker emits `done` | Result card shows transcript preview (scrollable), buttons: *Open folder*, *Copy text*, *New transcription*. |
| **Error** | Worker emits `error` | Red banner with message; Retry button. |

### 4.4 Drop zone behavior

- Outline dashed border, subtle background.
- On drag-over: border solidifies to accent color, background tints.
- On drop: validates extension; rejects with a toast if unsupported.
- Click anywhere on the zone opens a native file picker.

### 4.5 Model picker

- Radio buttons in a single row.
- Each option shows a tooltip on hover with size and approximate speed:
  - `tiny` — 75 MB, ~10× realtime on CPU
  - `base` — 145 MB, ~5× realtime on CPU
  - `small` — 484 MB, ~2× realtime on CPU
  - `medium` — 1.5 GB, ~1× realtime on CPU
  - `large` — 3.0 GB, ~0.3× realtime on CPU
- Already-downloaded models display a small ✓ badge.

### 4.6 Language picker

- Dropdown defaulting to "Auto-detect".
- Lists the 99 languages Whisper supports, alphabetized, searchable by typing.

### 4.7 Progress card

- Replaces the central area during transcription.
- Shows: current operation ("Extracting audio…", "Transcribing…"), percent bar, elapsed/remaining timers, current segment text streaming in (last ~3 lines).
- Cancel button returns to idle.

### 4.8 Result card

- Scrollable text area with the full transcript.
- Footer row: file path, word count, language detected, time taken.
- Buttons: **Open folder** (reveals output in OS file manager), **Copy all**, **New transcription**.

### 4.9 Settings panel (gear icon)

- Default output location (defaults to same folder as input).
- Default model.
- Compute device: Auto / CPU / GPU (CUDA).
- Compute precision: Auto / int8 / float16 / float32.
- Open model cache folder.
- Clear model cache.
- About / version / license.

### 4.10 Output file naming

For input `lecture.mp4`, outputs are written next to the source as:

```
lecture.txt
lecture.srt
lecture.vtt
```

If a file already exists, append `_1`, `_2`, etc.

---

## 5. Whisper Integration

### 5.1 Source

We use **faster-whisper**, a reimplementation of OpenAI Whisper using CTranslate2.

- Repository: <https://github.com/SYSTRAN/faster-whisper>
- PyPI: <https://pypi.org/project/faster-whisper/>
- License: MIT
- Original Whisper: <https://github.com/openai/whisper> (MIT, kept as reference; not bundled).

### 5.2 Why faster-whisper instead of reference Whisper

- 4× faster on CPU, 2× faster on GPU.
- Up to 50% lower memory.
- No PyTorch dependency (saves ~800 MB in the installer).
- Identical model weights and accuracy — uses the same Hugging Face checkpoints.
- Native int8 quantization for fast CPU inference.

### 5.3 Installation (development)

```bash
pip install faster-whisper==1.0.3
```

### 5.4 Model files

`faster-whisper` downloads converted models from Hugging Face on first use. They are cached at:

- Windows: `%USERPROFILE%\.cache\huggingface\hub`
- macOS / Linux: `~/.cache/huggingface/hub`

Model repository names:

| Friendly name | HF repo |
|---|---|
| tiny | `Systran/faster-whisper-tiny` |
| base | `Systran/faster-whisper-base` |
| small | `Systran/faster-whisper-small` |
| medium | `Systran/faster-whisper-medium` |
| large-v3 | `Systran/faster-whisper-large-v3` |

The first call to `WhisperModel("base")` triggers the download. We surface this download with our own progress UI by hooking `huggingface_hub`'s download callbacks.

### 5.5 Integration code pattern

```python
# core/transcriber.py
from faster_whisper import WhisperModel
from pathlib import Path

class Transcriber:
    def __init__(self, model_name: str, device: str = "auto",
                 compute_type: str = "auto"):
        self.model = WhisperModel(
            model_name,
            device=device,             # "cpu", "cuda", or "auto"
            compute_type=compute_type, # "int8", "float16", "float32", "auto"
        )

    def transcribe(self, audio_path: Path, language: str | None,
                   on_segment, on_progress):
        segments, info = self.model.transcribe(
            str(audio_path),
            language=language,         # None = auto-detect
            beam_size=5,
            vad_filter=True,           # skip long silences
            word_timestamps=False,
        )
        collected = []
        total = info.duration
        for seg in segments:
            collected.append(seg)
            on_segment(seg.text)
            on_progress(seg.end / total)
        return collected, info
```

### 5.6 Audio preprocessing

`faster-whisper` accepts video files directly because it links against PyAV/ffmpeg. We still ship a static `ffmpeg` binary as a fallback for unusual containers and to extract metadata (duration, codec) before transcription begins.

### 5.7 GPU support

- On systems with an NVIDIA GPU and CUDA 12 runtime present, `device="auto"` selects CUDA automatically.
- The bundled CTranslate2 shared library includes CUDA kernels; no separate PyTorch install required.
- AMD/Apple Silicon GPUs fall back to CPU; CTranslate2 has no Metal backend yet (as of this writing).

---

## 6. Build & Launch

### 6.1 Project structure

```
whisper-transcriber/
├── main.py
├── pyproject.toml
├── requirements.txt
├── ui/
│   ├── __init__.py
│   ├── app.py
│   ├── theme.py
│   └── components/
│       ├── drop_zone.py
│       ├── model_picker.py
│       ├── progress_card.py
│       └── result_card.py
├── core/
│   ├── __init__.py
│   ├── transcriber.py
│   ├── audio.py
│   ├── exporters.py
│   └── models.py
├── resources/
│   ├── icons/
│   ├── bin/
│   │   ├── ffmpeg-win.exe
│   │   ├── ffmpeg-mac
│   │   └── ffmpeg-linux
│   └── fonts/
├── installers/
│   ├── windows.iss          # Inno Setup
│   ├── macos_dmg.sh
│   └── linux_appimage.sh
└── tests/
```

### 6.2 Development setup

```bash
git clone <repo-url>
cd whisper-transcriber
python3.11 -m venv .venv
source .venv/bin/activate          # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

### 6.3 Building a distributable

**Windows:**
```bash
pyinstaller --noconfirm --windowed --icon=resources/icons/app.ico \
            --add-data "resources;resources" \
            --name "WhisperTranscriber" main.py
iscc installers/windows.iss        # produces signed .exe installer
```

**macOS:**
```bash
pyinstaller --noconfirm --windowed --icon=resources/icons/app.icns \
            --add-data "resources:resources" \
            --osx-bundle-identifier com.example.whispertranscriber \
            --name "WhisperTranscriber" main.py
codesign --deep --force --sign "Developer ID Application: …" \
         dist/WhisperTranscriber.app
bash installers/macos_dmg.sh       # creates and notarizes .dmg
```

**Linux:**
```bash
pyinstaller --noconfirm --windowed \
            --add-data "resources:resources" \
            --name "WhisperTranscriber" main.py
bash installers/linux_appimage.sh  # produces .AppImage
```

### 6.4 First-launch UX

1. User runs the installer (or double-clicks `.AppImage` / opens the `.app`).
2. App opens to the idle screen instantly (no model loaded yet).
3. User drops a file and clicks Transcribe.
4. App detects no `base` model is cached, shows the download progress card.
5. Once the model is ready, transcription begins automatically.

No terminal, no `pip install`, no PATH editing required from the end user.

### 6.5 Uninstall

- Windows: standard "Add or remove programs". Model cache remains in `%USERPROFILE%\.cache` unless removed via Settings → Clear model cache.
- macOS: drag app to Trash; cache as above.
- Linux: delete the AppImage.

---

## 7. Open questions / future enhancements

- Speaker diarization (e.g. integrate `pyannote.audio`).
- Batch mode: drop multiple files, queue them.
- Live editing of the transcript inside the result card with re-export.
- Apple Silicon GPU acceleration once CTranslate2 ships a Metal backend.
- Optional translation-to-English mode (Whisper supports this natively via `task="translate"`).
