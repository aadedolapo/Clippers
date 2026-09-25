# Viral Clip Finder → DaVinci Resolve

A local tool that finds short-clip candidates in a long video, ranks them, generates a scroll-stopping hook line and word-highlight captions for each, and sends your picks straight into DaVinci Resolve as a ready-to-edit timeline — reframed to 9:16 for Shorts/Reels/TikTok.

It's a local dev tool, not a hosted app: one Python backend, one browser tab, running on your own machine, talking to a DaVinci Resolve instance open on that same machine.

## Requirements

- Python 3.9+
- [ffmpeg](https://ffmpeg.org/) on your `PATH`
- DaVinci Resolve, **open with a project loaded**, on the same machine
- An [Anthropic API key](https://console.anthropic.com/) for the clip-scoring pass
- A way to get a good-quality source video file onto your machine — a third-party downloader (e.g. [Cobalt](https://cobalt.tools), 4K Video Downloader) or your own footage. This tool doesn't fetch video for you; you bring the file.

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

## Running it

1. Open DaVinci Resolve, with a project loaded.
2. Start the server:

   ```bash
   cd backend
   python main.py
   ```

3. Open **http://localhost:8000** in your browser.

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
   - Renders a **separate, ready-to-post mp4** into `output/` — same portrait crop, captioned in whichever style you picked for that clip (see **Caption styles** below)
6. **Switch to Resolve.** Your timeline is there, clips trimmed and reframed, ready to review, tighten, and export. Captions are *not* added to the Resolve timeline automatically — drag in your own animated subtitle template (Effects Library → Titles → Subtitles) once per timeline if you want native, editable captions there; it'll stick around for every clip you send to that timeline afterward.

## Daily workflow

The retention data behind this tool (see `RETENTION_UPGRADE_PLAN.md`) points at one bottleneck: too many viewers swipe away in the first second. The operating model this pushes toward is **create lots of candidates, post very few** — analyze several videos a day, then post only the day's actual best 1–2 clips, not a top-N from every video.

1. **Pick 3–4 source videos** that are on-lane (faith, men's growth, discipline, purpose). Prefer less-clipped speakers.
2. **Analyze all of them**, then open **Today's picks** at the top of the page — it ranks every clip from that day's sessions across all videos, drops anything that fails the hook check, and picks at most one per lane so the top 2 aren't both the same topic.
3. **Check the top 2 yourself**: does the first second actually make *you* stop?
4. **Send to Resolve**, finish, and post **1–2 per day**, spaced several hours apart. Optionally toggle a cold open (flash-forwards the clip's strongest line) or a Wisement outro (a short spoken close + follow CTA) per clip before sending.
5. **48h after posting**, click **Mark as posted** on the clip, paste the YouTube URL, and log stayed-to-watch % and views — or import a YouTube Studio CSV export (Content → Shorts → Export) weekly via the button next to Today's picks, which matches rows to already-posted clips automatically.
6. **Every 2 weeks**, run `python backend/performance.py report` (avg stayed % by lane, duration, and cold-open use) and adjust the weights by hand in `backend/scoring.py`'s `WEIGHTS`. Once you have 10+ posted clips with stats, the scorer automatically starts feeding itself the best/worst-performing hooks as examples.

## Caption styles

Pick a caption style per clip from the dropdown before sending to Resolve:

- **`minimal-faith`** (default) — short animated phrases (2–4 words at a time, rotating entrance animations) instead of word-by-word karaoke: Montserrat ExtraBold body, Playfair Display Bold Italic yellow emphasis word. Comes with its own edit-effects toggles alongside the caption dropdown:
  - **Zoom-out opener** (on by default) — scales in from 112% to 100% over the first ~1.7s, with a soft whoosh if one's configured (see **Music & SFX** below).
  - **B&W grade** (off by default) — a muted black-and-white look for the whole clip.
  - **Music bed** (off by default) — pick a mood, **slow** or **cinematic**; see below.
- `bold-white`, `yellow-pop`, `minimal` — the original word-by-word karaoke-highlight styles, still available if you prefer them.

See `backend/captions.py` if you want to tweak fonts, colors, timings, or add your own preset.

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

## Experimental: Fusion-native captions

`backend/fusion_captions.py` generates the `minimal-faith` captions as an **editable Fusion composition** on the Resolve timeline instead of a burned-in mp4, so you can tweak the animation by hand after sending. As of this writing it's **not reliable yet**: video content and 9:16 framing work, but caption timing/positioning breaks once the generated comp is attached to a real timeline clip, for reasons not yet fully root-caused (see the module's own docstring for what's confirmed vs. still open, and `docs/resolve_api_notes.md` / `docs/fusion_templates.md` for the underlying Fusion scripting notes and manual title-template build guide). Use the regular `minimal-faith` mp4 path for anything you actually intend to post.

`python backend/fusion_captions.py --demo` sends a test clip to a `WM Fusion Test` timeline (video + framing + music/SFX only) **without** attaching the broken captions, so it's safe to re-run for anything other than the captions themselves. Pass `--with-captions` to also attach the caption comp if you're picking up the debugging.

## Where things end up

| What | Where |
|---|---|
| Uploaded/copied source videos | `backend/downloads/` |
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
