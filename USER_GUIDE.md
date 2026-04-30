# Transcribe — User Guide

A local, transcript-driven video editor for Mac. You drop in a video, you
get a transcript, you edit it Descript-style by striking words. Pair it
with Claude Desktop and you have an AI editor that can propose cuts and
generate social-ready clips for you. Everything runs on your machine —
no uploads, no cloud APIs, no telemetry.

This guide gets you from nothing to a 9:16 highlight clip auto-cut from
a long-form podcast. About fifteen minutes the first time, less after.

## What you need

A Mac with Apple Silicon (M1 or later), running macOS 13 or later. Intel
Macs are untested.

Two apps:

- **Claude Code** — Anthropic's coding agent. It handles installing
  Transcribe and everything Transcribe depends on. See Anthropic's docs
  for the current install instructions.
- **Claude Desktop** — the chat app you'll use to direct Transcribe.
  Free download from Anthropic.

That's it. You don't need to install Python, git, ffmpeg, or anything
else. Claude Code installs whatever's missing on its own.

About 4 GB of free disk space. The AI transcription model is downloaded
once on first use (~150 MB), and rendered videos accumulate as you make
them.

## Setup

Open Claude Code and paste this prompt:

```
Set up Transcribe on this Mac from
https://github.com/TheCaffeineDuck/AI-Video-Editor. Clone the repo to
~/Desktop/Transcribe. If Homebrew, git, or Python 3.11 are missing,
install them. Create a Python 3.11 virtual environment at .venv inside
the project, install dependencies from requirements.txt, then launch
the GUI by running ./.venv/bin/python main_qt.py.
```

Claude Code will work through this for a few minutes. You'll see it
install whatever's missing, clone the repo, set up the environment, and
open the Transcribe window.

When the window opens, a dialog asks whether to connect Transcribe to
Claude Desktop. Click **Connect**.

A confirmation appears asking you to quit and relaunch Claude Desktop.
Do that — fully quit with Cmd-Q (closing the window is not the same;
macOS keeps the app running in the background). Then reopen it.

To verify: start a new chat in Claude Desktop. Transcribe's tools
should appear in the available tools for that chat. If they do, you're
done.

## Transcribing a video

Drop a video onto the Transcribe window — MP4, MOV, MKV, M4A, MP3, or
WAV. The transcribe pane runs the AI model over the audio. On an M4
Mac, expect roughly real-time on the first run (the model has to
download), faster on subsequent videos.

When it finishes, you'll see the transcript in the window. A file
named `yourvideo.transcribe.json` appears next to your source video
on disk. That file is the project — every word, every timestamp, the
editing state. Plain JSON, hand-readable.

For the AI workflow you don't need to do anything else in the GUI.
You can close the window. The `.transcribe.json` is what Claude
works with.

## The AI highlight workflow

In Claude Desktop, start a new chat and point Claude at the transcript:

> I have a transcribed podcast at
> `/Users/me/Desktop/podcasts/episode-42.transcribe.json`. Find me five
> highlights that would work as social clips — self-contained moments,
> a story or strong take or funny exchange. Don't make them too long.

Claude reads the transcript, picks moments, and saves them as proposal
files. You'll see Claude's reasoning in the chat — which moments it
picked and why. The proposals appear in a `.highlights` folder next to
your video.

To render them, two options:

**Through Claude.** Tell Claude which ones to render — "render
highlights two and four" or "render all of them." Each 30-second
highlight takes about 45 seconds to render on an M4. The mp4 files
appear in the highlights folder when done.

**Through the Transcribe GUI.** Open the project (File → Open, pick
the `.transcribe.json`). The Highlights panel shows every proposed
highlight as a card. Click Render on the ones you want. Click Open
once a render finishes to preview it in QuickTime.

Use Claude when you trust the picks and want fire-and-forget. Use the
GUI when you want to eyeball the picks before committing render time.

The output mp4s are 1080×1920, H.264 + AAC, ready to upload directly
to TikTok, Reels, or Shorts.

## What to expect from the AI

Claude is good at picking moments with clear narrative arcs — setup,
beat, payoff. Strong one-liners with enough lead-in to land. Funny
exchanges where the rhythm lives in the words.

It's mediocre at moments where the value is non-verbal — reactions,
gestures, visual jokes. It's working from text, not video.

It tends to err short. If your podcast is about ideas rather than
quick exchanges, ask explicitly for "longer narrative highlights, 60
to 90 seconds." The default trends toward 15-30 second clips.

It doesn't know what the camera is doing. Picks are based on what was
said, not how the shot was framed. If a moment depends on a specific
visual, you'll need to ask explicitly or pick that one yourself.

The 9:16 reframe uses face detection to keep the speaker centered in
the vertical crop. On standard interview shots facing the camera, it
works well. On profile shots, low light, or multi-face shots, it
falls back to a static center crop. Each rendered highlight gets a
sidecar metadata file noting which path was used; if a clip looks
weird, that's the first thing to check.

## The other AI workflow: cut editing

The highlight path is one use of the integration. The other is editing
the main video itself — removing fillers, long silences, repetitions.
Same shape, but with a human review step that highlights skip.

Open the Transcribe GUI and load your transcribed video. In Claude
Desktop, ask:

> Read the document at
> `/Users/me/Desktop/podcasts/episode-42.transcribe.json` and propose
> cuts to remove filler words, long silences, and false starts.

Claude analyzes and writes a proposal file. In the GUI, open Edit →
Review Proposal. Each proposed cut shows the transcript snippet it
affects, with Accept and Reject buttons. Reject prompts you for a
one-word reason — "keep" works fine.

When you've decided on every cut, hit Apply. The cuts land in the
project. Export from File → Export and you get a rendered mp4 with
the cuts applied.

Why human review here and not for highlights: cuts on the main video
are destructive in a way social clips aren't. Highlights are throwaway
derivatives — if Claude picks one you don't like, you just don't render
it. Cuts edit the master, so each one is worth a glance.

## Where things live on disk

Everything is local and visible. There's no hidden state.

- `yourvideo.mp4` — your source. Never touched, ever.
- `yourvideo.transcribe.json` — the project. Words, timestamps, edit
  log, timeline.
- `yourvideo.transcribe.json.proposals/` — Claude's cut proposals,
  one file each, plus a record of which were applied.
- `yourvideo.transcribe.json.highlights/` — highlight specs, rendered
  mp4s, and render metadata.

To start over on a video, delete the `.transcribe.json` and the two
sidecar folders. Your source is untouched.

## Re-launching Transcribe

Two options.

**Ask Claude Code.** Say "open Transcribe." It runs the GUI. Works
every time, no setup beyond what you've already done.

**Make it a double-click.** Ask Claude Code once to create a Launch
Transcribe shortcut on your Desktop. It writes a small file you can
double-click to open the GUI directly.

A proper Mac app with a real icon and Dock presence is on the roadmap
but not built yet.

## If something goes wrong

The simplest answer: tell Claude Code what's wrong. It can read logs,
debug the install, and fix things in place. "The Transcribe GUI won't
open" or "Claude Desktop can't see Transcribe" both work as starting
prompts.

Specific common issues:

**Claude Desktop doesn't see Transcribe.** Most often, Claude Desktop
wasn't fully quit before the relaunch. Cmd-Q from inside Claude
Desktop (don't just close the window), wait a few seconds, reopen.
Second most likely: the connect dialog was dismissed. Open the
Transcribe GUI, go to Help → Connect to Claude Desktop, click
through.

**Transcription is slow on the first run.** Normal — the AI model is
downloading and caches are warming. Subsequent videos go faster. If
it stays slow, check Activity Monitor; something else on the machine
may be saturating the CPU.

**A render fails.** Usually because the source video moved or got
renamed after transcribing — the project records the original path
and looks for the file there. Move it back, or re-transcribe from the
new location.

**A highlight came out framed weird.** Check the render metadata file
in the highlights folder. If face detection fell back to a center
crop, pick a different span — that one had no clear face for the
detector to lock onto. Dynamic speaker tracking is a roadmap item; for
now, the workaround is choosing a span with a clearer shot.

For anything else, ask Claude Code. It has access to the project's
logs and can usually diagnose problems in seconds.
