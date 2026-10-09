# Viral Clip Finder → DaVinci Resolve

A local tool that finds short-clip candidates in a long video, ranks them, generates a scroll-stopping hook line and word-highlight captions for each, and sends your picks straight into DaVinci Resolve as a ready-to-edit timeline — reframed to 9:16 for Shorts/Reels/TikTok.

It's a local dev tool, not a hosted app: one Python backend, one browser tab, running on your own machine, talking to a DaVinci Resolve instance open on that same machine.

## Requirements

- Python 3.9+
- [ffmpeg](https://ffmpeg.org/) on your `PATH`
- DaVinci Resolve, **open with a project loaded**, on the same machine
- An [Anthropic API key](https://console.anthropic.com/) for the clip-scoring pass
- A way to get a good-quality source video file onto your machine — a third-party downloader (e.g. [Cobalt](https://cobalt.tools), 4K Video Downloader) or your own footage. This tool doesn't fetch video for you; you bring the file. If you use 4K Video Downloader, the tool can watch its download folder and process new videos automatically (see **Agent mode** below).

## Setup

```bash
git clone <this repo>
cd Clippers
python3 -m venv venv
source venv/bin/activate
pip install -r backend/requirements.txt
cp backend/.env.example backend/.env
```

Open `backend/.env` and fill in your key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Optionally, to have new downloads processed automatically, point the watch folder at 4K Video Downloader's output folder (see **Agent mode** below):

```
CLIPPER_WATCH_DIR=/Users/<you>/Movies/4K Video Downloader+
```

## Running it

1. Open DaVinci Resolve, with a project loaded.
2. Start the server:

   ```bash
   cd backend
   python main.py
   ```

3. Open **http://localhost:8000** in your browser. The agent's live activity page is at **http://localhost:8000/agent**.

That one server runs everything — the UI, the agent, and the watch folder. There's nothing else to start.

## Usage

1. **Get your video file.** Download the source video with whatever tool gives you the quality you want, and note where it lands on disk.
2. **Upload it.** In the browser, click the **file picker** and choose the video. Give it a title if you want one different from the filename.
3. **Analyze.** Click **Analyze**. The backend transcribes the audio, then scores overlapping windows of the transcript for how well each would work as a standalone short clip — this takes a few minutes depending on video length.
4. **Review the candidates.** Each clip card shows:
   - Timestamp range and duration
   - A 0–10 score and a one-line reason ("strong hook line", "punchline lands", etc.)
   - A generated **hook** — a short, punchy line meant to be typed into a title overlay at the very start of the clip. Click **Copy** and paste it into your own title template in Resolve.
   - The transcript excerpt for that window
5. **Select clips and send to Resolve.** Check the ones you want, click **Send to Resolve**. For each clip, the backend:
   - Imports the source file into Resolve's media pool (once per video)
   - Creates (or reuses) a timeline named `Viral Clips - <title>`, set to 9:16 portrait
   - Appends the trimmed clip, zoomed to fill the portrait frame (no letterboxing)
   - Runs Resolve's AI Smart Reframe on it, so a moving speaker stays framed instead of sitting at a fixed crop (requires Resolve **Studio**; free Resolve still gets everything else)
   - Renders a **separate, ready-to-post mp4** into `output/` — same portrait crop, with whichever edit effects you picked for that clip (zoom opener, B&W grade, music bed) applied (see **Clip style** below)
6. **Switch to Resolve.** Your timeline is there, clips trimmed and reframed, ready to review, tighten, and export. Captions are *not* added automatically anywhere (not the timeline, not the `output/` mp4) — drag in your own animated subtitle template (Effects Library → Titles → Subtitles) once per timeline if you want native, editable captions there; it'll stick around for every clip you send to that timeline afterward.

## Agent mode (watch folder)

Instead of uploading and analyzing by hand, the tool can pick up videos as soon as 4K Video Downloader finishes them and let an agent choose the clips.

**How it works:**

1. **Watch folder** (`backend/watcher.py`) — while the server is running, it checks `CLIPPER_WATCH_DIR` every 10 seconds. A video is picked up once its file size stops changing (so half-finished downloads are left alone), and each file is processed exactly once, even across restarts.
2. **Transcribe and score** — same as a manual run, but the scorer proposes up to 10 candidates instead of the top 3.
3. **Agent review** (`backend/agent.py`) — Claude goes through the candidates with three tools:
   - `read_transcript` — reads what's said before and after a candidate
   - `adjust_clip` — moves a clip's start/end (snapped to transcript segments, kept to 15–45s, no overlaps) so it opens on a line that stands alone and ends right after the payoff
   - `select_clip` — picks it, with a one-sentence reason

   It picks 0–6 clips (usually 1–2; picking none is allowed for a weak video), skips clips that repeat the same point, and writes a summary of what it picked, what it rejected, and what you should double-check.
4. **You review and send to Resolve** — the agent never touches Resolve. Its picks are saved like any other session: **refresh** the main page and they appear as a saved video (and in **Today's picks**). Tick the ones you want, set B-roll/music/cold open as usual, and click **Send to Resolve**.

**Watching it work:** open **http://localhost:8000/agent**. It lists every job (including ones the watch folder started) and shows a live feed of each run — steps, the agent's reasoning, every transcript read, re-cut and pick, and the final summary. You can also start an agent run there on any video already in `backend/downloads/`.

To run the agent on a manual upload instead, `POST /analyze` with `"agent": true`. From the command line: `python backend/agent.py <video file>`.

**Things to know:**

- **The first time the watcher starts, it ignores the videos already in the folder**, so pointing it at a folder full of old downloads doesn't start a pile of jobs. Only downloads that arrive after that are processed.
- **Downloads that land while the server is off are picked up on the next start.**
- **Don't point `CLIPPER_WATCH_DIR` at `backend/downloads/`** — the pipeline copies videos into that folder, so it would pick up its own copies. The server refuses to start if you do.
- **Don't run two servers with the watch folder set** — both would process the same download.
- **The agent page's job list and activity feeds reset on restart.** Saved picks survive, but **Send to Resolve** only works for videos analyzed since the server last started (same as manual runs).
- If the agent run fails, you get the scorer's top 3 instead, with a note saying so.
- B-roll is not part of the agent — it's still the per-clip button in the main UI.

## Daily workflow

The retention data behind this tool (see `RETENTION_UPGRADE_PLAN.md`) points at one bottleneck: too many viewers swipe away in the first second. The operating model this pushes toward is **create lots of candidates, post very few** — analyze several videos a day, then post only the day's actual best 1–2 clips, not a top-N from every video.

1. **Pick 3–4 source videos** that are on-lane (faith, men's growth, discipline, purpose). Prefer less-clipped speakers.
2. **Analyze all of them**, then open **Today's picks** at the top of the page — it ranks every clip from that day's sessions across all videos, drops anything that fails the hook check, and picks at most one per lane so the top 2 aren't both the same topic.
3. **Check the top 2 yourself**: does the first second actually make *you* stop?
4. **Send to Resolve**, finish, and post **1–2 per day**, spaced several hours apart. Optionally toggle a cold open (flash-forwards the clip's strongest line) or a Wisement outro (a short spoken close + follow CTA) per clip before sending.
5. **48h after posting**, click **Mark as posted** on the clip, paste the YouTube URL, and log stayed-to-watch % and views — or import a YouTube Studio CSV export (Content → Shorts → Export) weekly via the button next to Today's picks, which matches rows to already-posted clips automatically.
6. **Every 2 weeks**, run `python backend/performance.py report` (avg stayed % by lane, duration, and cold-open use) and adjust the weights by hand in `backend/scoring.py`'s `WEIGHTS`. Once you have 10+ posted clips with stats, the scorer automatically starts feeding itself the best/worst-performing hooks as examples.

## Clip style

This tool no longer burns in any captions — that's on you now: drag your own animated subtitle template onto the clip in Resolve by hand (Effects Library → Titles → Subtitles) once you're in the timeline. The "Clip style" dropdown per clip picks which *edit-effects bundle* renders instead:

- **Minimal Faith** (default) — zoom-out opener, optional B&W grade, and a music bed, but no cold open (its effects pipeline doesn't have a cold-open crossfade path built for it):
  - **Zoom-out opener** (on by default) — scales in from 112% to 100% over the first ~1.7s, with a soft whoosh if one's configured (see **Music & SFX** below).
  - **B&W grade** (off by default) — a muted black-and-white look for the whole clip.
  - **Music bed** (off by default) — pick a mood, **slow** or **cinematic**; see below.
- **Plain** — just the reframed clip, no zoom/grade/music bundle, but eligible for the cold-open toggle above it.

See `backend/captions.py` if you want to tweak fonts, colors, or timings for a template you build yourself, or resurrect burned-in captions (the karaoke rendering code is still there, just no longer wired into `/send-to-resolve`).

## Music & SFX

Both are opt-in per clip and skip silently if unconfigured — nothing breaks if you leave these blank.

- **Music bed**: tracks are picked at random from your own local library by mood, avoiding the last 5 used (so back-to-back clips don't repeat). Point `MUSIC_DIRS` in `backend/.env` at your mood folders:
  ```
  MUSIC_DIRS=slow=~/Music/Music/slowmusic,cinematic=~/Music/Music/cinematic
  ```
  Mixed under the voice at roughly −15 LUFS (voice) / −28 LUFS (music), ducked further via sidechain compression whenever the speaker talks.
- **Whoosh SFX**: plays on the zoom-out opener automatically when available. Point `SFX_DIR` in `backend/.env` at a sound effects library on disk (e.g. a DaVinci Resolve Sound Library folder):
  ```
  SFX_DIR=~/Downloads/SFX
  ```
  Indexed once into `backend/data/sfx_index.json` (whoosh/riser/impact, sorted by filename) and re-used after that — delete that file to force a re-index if you add new files.

## Where things end up

| What | Where |
|---|---|
| Uploaded/copied source videos (including watch-folder pickups) | `backend/downloads/` |
| Watch-folder record of already-processed files | `backend/data/watch_state.json` |
| Captioned, ready-to-post clips | `output/` |
| Resolve timelines | inside your open Resolve project, named `Viral Clips - <title>` |
| Saved clip metadata (hook/title/scorecard) | `backend/data/saved_clips.json` |
| Posted-clip performance log | `backend/data/performance.json` |
| Bundled caption fonts (Montserrat, Playfair Display) | `backend/assets/fonts/` |
| Music-pick history (avoids repeats) | `backend/data/music_history.json` |
| SFX file index | `backend/data/sfx_index.json` |

## Notes

- This is a single-user local tool — no auth, no database, job state resets on server restart.
- Resolve must already be open with a project loaded before you click **Send to Resolve**.
- Scoring runs several Claude API calls concurrently per video; cost scales with video length.
- The agent uses `claude-opus-5-5` by default, while scoring and the other Claude calls use `claude-sonnet-5`. To run the agent on the cheaper model, set `CLAUDE_AGENT_MODEL=claude-sonnet-5` in `backend/.env`. An agent run takes about 30 seconds on top of transcribing and scoring.
