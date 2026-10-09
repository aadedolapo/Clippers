"""
FastAPI wrapper around the pipeline (PRD section 8): /analyze, /send-to-resolve,
/status/{job_id}. In-memory job state only — no database, single user, localhost.
"""

import hashlib
import logging
import os
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Literal, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("viral_clipper")

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from pipeline import DOWNLOAD_DIR, download_video, use_local_file, unique_download_path, transcribe
from scoring import score_video
import agent
import beat_plan
import broll
import resolve_integration
import captions
import commentary
import commentary_render
import music
import performance
import sfx
import storage
import watcher

EXECUTOR = ThreadPoolExecutor(max_workers=2)
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

# 4K Video Downloader's output folder. Unset means no watching; videos can
# still be analyzed by hand through the UI.
WATCH_DIR = os.environ.get("CLIPPER_WATCH_DIR")


def _submit_job(local_file: Optional[str] = None, url: Optional[str] = None,
                title: Optional[str] = None, source: Optional[str] = None,
                use_agent: bool = False) -> str:
    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "url": url, "local_file": local_file, "agent": use_agent,
                        "created_at": time.time(), "events": []}
    EXECUTOR.submit(_run_analysis, job_id, url, local_file, title, source, use_agent)
    return job_id


@asynccontextmanager
async def lifespan(_app):
    if WATCH_DIR:
        # use_local_file copies into DOWNLOAD_DIR, so watching that folder
        # would pick up its own copies as new videos.
        if os.path.realpath(os.path.expanduser(WATCH_DIR)) == os.path.realpath(DOWNLOAD_DIR):
            raise RuntimeError("CLIPPER_WATCH_DIR must not be backend/downloads; point it at 4K Video Downloader's folder")
        watcher.start_watcher(WATCH_DIR, lambda path: _submit_job(local_file=path, use_agent=True))
    yield


app = FastAPI(title="Viral Clip Finder", lifespan=lifespan)

JobStatus = Literal["queued", "downloading", "transcribing", "scoring", "done", "error"]


class AnalyzeRequest(BaseModel):
    url: Optional[str] = None
    local_file: Optional[str] = None
    title: Optional[str] = None
    # Church/speaker attribution — not required to find clips, but logged
    # per video for future permission outreach and claim tracing (PRD
    # pipeline step 6).
    source: Optional[str] = None
    # Let the orchestrator agent (agent.py) review and pick clips instead of
    # taking the scorer's top 3 as-is. Watch-folder jobs always use it.
    agent: bool = False


class BrollSelection(BaseModel):
    provider: str
    id: Optional[str] = None
    preview_url: str
    query: str
    offset: Optional[float] = None
    duration: Optional[float] = None


class BeatPlanItem(BaseModel):
    beat_type: str
    start: float
    end: float
    note: Optional[str] = None
    emphasis_word: Optional[str] = None


class ClipSelection(BaseModel):
    start: float
    end: float
    broll: list[BrollSelection] = []
    # Editorial beat structure (beat_plan.py) fetched on demand per clip and
    # resent here, same round-trip pattern as `broll` above — empty means
    # "no beat plan requested for this clip," not an error.
    beat_plan: list[BeatPlanItem] = []
    # Per-clip on/off toggle for the cold open a clip may have — defaults to
    # on when the clip has one, since the retention upgrade is opt-out, not
    # opt-in (the plan's UI is a checkbox to turn it OFF, not on).
    use_cold_open: bool = True
    # Add a spoken Wisement outro (retention upgrade plan phase 6) — opt-in,
    # since it's a new editorial layer, not a bug-fix default like
    # use_cold_open. Intro is deliberately not offered: anything before the
    # hook hurts retention, so only the outro is ever wired in.
    with_commentary: bool = False
    # A short on-screen comment-bait question after the clip ends — text
    # only, never spoken, so it never triggers a second ElevenLabs TTS call
    # even when with_commentary is also on (both reuse the same
    # commentary.py call; see send_to_resolve).
    comment_bait: bool = False
    # "Minimal Faith" style (retention upgrade plan phase 7) — the new
    # default caption preset. use_cold_open is silently skipped for this
    # preset: it renders via its own effects pipeline (zoom/grade/music),
    # which doesn't have a cold-open crossfade path built for it yet.
    caption_preset: str = captions.MINIMAL_FAITH_PRESET
    zoom_opener: bool = True
    bw_grade: bool = False
    music_bed: bool = False
    music_mood: Literal["slow", "cinematic"] = "slow"


class SendToResolveRequest(BaseModel):
    video_id: str
    clips: list[ClipSelection]


class CopyrightStatusRequest(BaseModel):
    status: Literal["unknown", "cleared"]


class PerformanceRequest(BaseModel):
    video_id: str
    start: float
    end: float
    youtube_url: Optional[str] = None
    posted_at: Optional[float] = None
    stayed_to_watch_pct: Optional[float] = None
    views_48h: Optional[int] = None
    views_7d: Optional[int] = None
    likes: Optional[int] = None
    subs_gained: Optional[int] = None


def _set_job(job_id: str, **fields):
    with JOBS_LOCK:
        JOBS[job_id].update(fields)


def _job_event(job_id: str, kind: str, text: str):
    """Append to a job's activity feed, shown live on the /agent page."""
    with JOBS_LOCK:
        JOBS[job_id].setdefault("events", []).append({"t": time.time(), "kind": kind, "text": text})


def _serialize_clip(c: dict) -> dict:
    return {
        "start": c["start"],
        "end": c["end"],
        "duration": round(c["end"] - c["start"], 1),
        "score": round(c["combined_score"], 3),
        "reason": c["reason"],
        "text": c["text"],
        "emphasis_words": c["emphasis_words"],
        "hook": c["hook"],
        "pinned_line": c.get("pinned_line", ""),
        "pinned_line_emphasis": c.get("pinned_line_emphasis", []),
        "seo_title": c["seo_title"],
        "seo_description": c["seo_description"],
        "scorecard": c["scorecard"],
        "cold_open": c.get("cold_open"),
        "agent_note": c.get("agent_note"),
    }


def _serialize_rejected(c: dict) -> dict:
    return {
        "start": c["start"],
        "end": c["end"],
        "duration": round(c["end"] - c["start"], 1),
        "text": c["text"],
        "reject_reason": c["reject_reason"],
        "scorecard": c["scorecard"],
    }


def _run_analysis(
    job_id: str,
    url: Optional[str],
    local_file: Optional[str] = None,
    title: Optional[str] = None,
    source: Optional[str] = None,
    use_agent: bool = False,
):
    try:
        _set_job(job_id, status="downloading")
        _job_event(job_id, "stage", f"Loading {local_file or url}")
        info = use_local_file(local_file, title=title) if local_file else download_video(url)
        filepath = info["_filepath"]
        video_id = info["id"]
        title = info.get("title", video_id)

        _set_job(job_id, status="transcribing", video_id=video_id, title=title, filepath=filepath, source=source)
        _job_event(job_id, "stage", "Transcribing with faster-whisper (the slow part on CPU)")
        segments = transcribe(filepath)
        _job_event(job_id, "stage", f"Transcribed {len(segments)} segments, {segments[-1]['end'] / 60:.1f} min of speech" if segments else "Transcript is empty")

        _set_job(job_id, status="scoring", segments=segments)
        agent_summary = None
        if use_agent:
            clips, rejected, agent_summary = agent.run_agent(
                segments, filepath, title, source=source,
                on_event=lambda kind, text: _job_event(job_id, kind, text),
            )
        else:
            # Top 3 per video — 8 was producing more candidates than were
            # actually worth reviewing per video.
            clips, rejected = score_video(segments, filepath, top_n=3)

        _set_job(job_id, status="done", clips=clips, rejected=rejected, agent_summary=agent_summary)
        # Persist hook/title/description (and timing) to disk immediately so
        # they survive a browser refresh even though JOBS is in-memory only.
        copyright_status = storage.save_session(
            video_id, title, [_serialize_clip(c) for c in clips], source=source
        )
        _set_job(job_id, copyright_status=copyright_status)
    except Exception as e:
        logger.exception(f"analyze job {job_id} failed")
        _set_job(job_id, status="error", error=str(e))
        _job_event(job_id, "error", str(e))


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    """Save an uploaded video file into downloads/ so it can be passed as
    `local_file` to /analyze — lets the frontend offer a real file picker
    instead of requiring the user to type an absolute path."""
    stem = os.path.splitext(file.filename or "upload")[0]
    ext = os.path.splitext(file.filename or "")[1] or ".mp4"
    video_id, dest = unique_download_path(stem, ext)

    with open(dest, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)

    return {"local_file": dest, "title": stem}


@app.post("/analyze")
def analyze(req: AnalyzeRequest):
    if bool(req.url) == bool(req.local_file):
        raise HTTPException(status_code=400, detail="Provide exactly one of 'url' or 'local_file'")

    job_id = _submit_job(local_file=req.local_file, url=req.url, title=req.title,
                         source=req.source, use_agent=req.agent)
    return {"job_id": job_id}


@app.get("/jobs")
def list_jobs():
    """Every job this server process has run, newest last — the only way to
    see watch-folder jobs, which no browser tab started."""
    with JOBS_LOCK:
        jobs = [
            {"job_id": jid, "status": j["status"], "title": j.get("title") or os.path.basename(j.get("local_file") or j.get("url") or ""),
             "video_id": j.get("video_id"), "agent": j.get("agent", False), "error": j.get("error"),
             "created_at": j.get("created_at")}
            for jid, j in JOBS.items()
        ]
    return {"jobs": jobs, "watch_dir": WATCH_DIR}


@app.get("/local-videos")
def local_videos():
    """Videos already in backend/downloads, for starting an agent run from
    the /agent page without going through 4K Video Downloader."""
    videos = []
    for name in sorted(os.listdir(DOWNLOAD_DIR)):
        path = os.path.join(DOWNLOAD_DIR, name)
        if not os.path.isfile(path) or os.path.splitext(name)[1].lower() not in watcher.VIDEO_EXTS:
            continue
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=10,
            )
            minutes = float(out.stdout.strip()) / 60
        except (subprocess.SubprocessError, ValueError):
            minutes = 0.0
        videos.append({"name": name, "path": path, "minutes": minutes})
    return {"videos": videos}


@app.get("/agent")
def agent_page():
    return FileResponse(os.path.join(FRONTEND_DIR, "agent.html"))


@app.get("/status/{job_id}")
def status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    response = {"status": job["status"], "agent": job.get("agent", False), "title": job.get("title"),
                "events": job.get("events", [])}
    if job["status"] == "error":
        response["error"] = job.get("error")
    if job["status"] == "done":
        response["video_id"] = job["video_id"]
        response["title"] = job["title"]
        response["source"] = job.get("source")
        response["copyright_status"] = job.get("copyright_status", storage.DEFAULT_COPYRIGHT_STATUS)
        response["clips"] = [_serialize_clip(c) for c in job["clips"]]
        response["rejected"] = [_serialize_rejected(c) for c in job.get("rejected", [])]
        response["agent_summary"] = job.get("agent_summary")
    return response


@app.get("/saved-clips")
def saved_clips():
    """All previously analyzed sessions' hook/title/description data, so the
    frontend can restore them after a browser refresh."""
    return {"sessions": storage.load_sessions()}


def _clip_key(c: dict) -> tuple:
    return (c["video_id"], c["start"], c["end"])


@app.get("/shortlist")
def shortlist(date: str, n: int = 2):
    """Retention upgrade plan phase 4: rank every clip saved from `date`'s
    sessions (across all videos analyzed that day) by combined score, and
    surface only the day's actual best 1-2 picks instead of a per-video top
    N — the new operating model is "create lots of candidates, post very
    few." Every persisted clip already passed the hook check (scoring.py
    never lets a hook_pass=false clip reach storage), so no further
    filtering is needed there.

    Diversity: at most one pick per lane in the top `n`, so a single strong
    video can't monopolize the day's picks — falls back to filling
    remaining slots by score alone if there aren't `n` distinct lanes
    available. `backups` is the next 5 candidates that didn't make the cut."""
    sessions = storage.load_sessions_for_date(date)
    pool = [
        {**c, "video_id": s["video_id"], "title": s["title"]}
        for s in sessions
        for c in s["clips"]
    ]
    pool.sort(key=lambda c: c["score"], reverse=True)

    picks: list[dict] = []
    used_lanes: set = set()
    picked_keys: set = set()
    for c in pool:
        if len(picks) >= n:
            break
        lane = c.get("scorecard", {}).get("lane", "other")
        if lane in used_lanes:
            continue
        picks.append(c)
        used_lanes.add(lane)
        picked_keys.add(_clip_key(c))

    if len(picks) < n:
        for c in pool:
            if len(picks) >= n:
                break
            if _clip_key(c) in picked_keys:
                continue
            picks.append(c)
            picked_keys.add(_clip_key(c))

    backups = [c for c in pool if _clip_key(c) not in picked_keys][:5]
    return {"date": date, "picks": picks, "backups": backups}


def _find_persisted_clip(video_id: str, start: float, end: float) -> Optional[dict]:
    for s in storage.load_sessions():
        if s["video_id"] != video_id:
            continue
        for c in s["clips"]:
            if abs(c["start"] - start) < 0.5 and abs(c["end"] - end) < 0.5:
                return c
    return None


@app.post("/performance")
def mark_performance(req: PerformanceRequest):
    """Retention upgrade plan phase 5: mark a clip as posted (typically just
    youtube_url on the first call) and/or record its stats. clip_snapshot
    comes from storage rather than the in-memory JOBS dict so this still
    works after a server restart, same as /saved-clips."""
    clip = _find_persisted_clip(req.video_id, req.start, req.end)
    record = performance.add_or_update_record(
        video_id=req.video_id,
        start=req.start,
        end=req.end,
        youtube_url=req.youtube_url,
        posted_at=req.posted_at,
        stayed_to_watch_pct=req.stayed_to_watch_pct,
        views_48h=req.views_48h,
        views_7d=req.views_7d,
        likes=req.likes,
        subs_gained=req.subs_gained,
        clip_snapshot=clip,
    )
    return record


@app.get("/performance")
def list_performance():
    return {"records": performance.load_records()}


@app.post("/performance/import-csv")
async def import_performance_csv(file: UploadFile = File(...)):
    """Accepts a YouTube Studio Shorts export (Content -> Shorts -> Export)
    and updates whichever already-posted records its rows match."""
    content = (await file.read()).decode("utf-8", errors="replace")
    return performance.import_csv(content)


@app.post("/copyright-status/{video_id}")
def update_copyright_status(video_id: str, req: CopyrightStatusRequest):
    """PRD §14 compliance layer: a human explicitly marking a source cleared
    (or reverting it to unknown) — the system never sets this to "cleared"
    on its own."""
    try:
        record = storage.set_copyright_status(video_id, req.status)
    except KeyError:
        raise HTTPException(status_code=404, detail="video not found")

    # Keep the in-memory job in sync too — JOBS holds a live reference to
    # the same dict _run_analysis populated, so this mutation is visible to
    # any later /status poll for this job without a second lookup path.
    job = _find_job_by_video_id(video_id)
    if job is not None:
        with JOBS_LOCK:
            job["copyright_status"] = req.status

    return {"video_id": video_id, "copyright_status": record["copyright_status"]}


@app.post("/send-to-resolve")
def send_to_resolve(req: SendToResolveRequest):
    job = _find_job_by_video_id(req.video_id)
    if job is None:
        raise HTTPException(status_code=404, detail="video not found; run /analyze first")

    results = []
    for clip in req.clips:
        matched = next(
            (c for c in job["clips"] if abs(c["start"] - clip.start) < 0.5 and abs(c["end"] - clip.end) < 0.5),
            None,
        )
        try:
            emphasis_words = matched.get("emphasis_words", []) if matched is not None else []
            pinned_line = matched.get("pinned_line", "") if matched is not None else ""
            pinned_line_emphasis = matched.get("pinned_line_emphasis", []) if matched is not None else []
            # minimal-faith renders through its own effects pipeline
            # (zoom/grade/music), which has no cold-open crossfade path
            # built for it yet — skip rather than silently drop coverage
            # elsewhere.
            is_minimal_faith = clip.caption_preset == captions.MINIMAL_FAITH_PRESET
            cold_open = (
                matched.get("cold_open")
                if matched is not None and clip.use_cold_open and not is_minimal_faith
                else None
            )

            # Picked once so the Resolve timeline's audio tracks and the
            # rendered deliverable mp4 (below) use the exact same files,
            # rather than each independently rolling a different random pick.
            music_path = music.pick_track(clip.music_mood) if is_minimal_faith and clip.music_bed else None
            whoosh_path = sfx.pick_whoosh() if is_minimal_faith and clip.zoom_opener else None

            broll_clips = []
            for sel in clip.broll:
                try:
                    local_path = _download_broll_selection(sel)
                    broll_clips.append({"filepath": local_path, "offset": sel.offset, "duration": sel.duration})
                except Exception:
                    logger.exception(f"B-roll download failed for {sel.preview_url}")

            # Wisement outro (phase 6): rendered once up front so the same
            # file can both be appended to the Resolve timeline (which
            # references it by path, so it must outlive this request) and
            # concatenated into the standalone deliverable mp4 below —
            # rendering it twice would mean a second TTS call for no reason.
            commentary_close_path = None
            commentary_intermediates: list[str] = []
            # Text-only, never spoken — comment_bait alone doesn't need TTS,
            # so it shares with_commentary's single Claude call (both fields
            # come back from the same commentary.py response) but never adds
            # a second ElevenLabs call on top of it.
            comment_bait_question = None
            if matched is not None and (clip.with_commentary or clip.comment_bait):
                commentary_result = commentary.generate_commentary_for_clips(
                    [matched], video_title=job["title"], source=job.get("source"),
                )[0]
                if clip.comment_bait:
                    comment_bait_question = commentary_result.get("comment_bait_question") or None
                if clip.with_commentary:
                    close_text = commentary_result.get("commentary_close")
                    if close_text:
                        base = os.path.splitext(os.path.basename(job["filepath"]))[0]
                        tag = f"{base}_{clip.start:.1f}-{clip.end:.1f}_outro"
                        close_render = commentary_render.render_commentary_segment(
                            close_text, job["filepath"], max(0.0, clip.end - 0.5), captions.OUTPUT_DIR, tag,
                        )
                        commentary_close_path = close_render["path"]
                        commentary_intermediates = close_render["intermediates"]

            if broll_clips:
                resolve_integration.send_clip_with_broll_to_resolve(
                    filepath=job["filepath"],
                    title=job["title"],
                    start=clip.start,
                    end=clip.end,
                    broll_clips=broll_clips,
                    cold_open=cold_open,
                    music_path=music_path,
                    whoosh_path=whoosh_path,
                )
            elif commentary_close_path:
                resolve_integration.send_clip_with_commentary_to_resolve(
                    filepath=job["filepath"],
                    title=job["title"],
                    start=clip.start,
                    end=clip.end,
                    close_path=commentary_close_path,
                    cold_open=cold_open,
                    music_path=music_path,
                    whoosh_path=whoosh_path,
                )
            else:
                resolve_integration.send_clip_to_resolve(
                    filepath=job["filepath"],
                    title=job["title"],
                    start=clip.start,
                    end=clip.end,
                    cold_open=cold_open,
                    music_path=music_path,
                    whoosh_path=whoosh_path,
                )

            caption_path = None
            if matched is not None and is_minimal_faith:
                body_path = captions.render_minimal_faith_clip(
                    filepath=job["filepath"],
                    segments=job["segments"],
                    start=clip.start,
                    end=clip.end,
                    emphasis_words=emphasis_words,
                    zoom_opener=clip.zoom_opener,
                    bw_grade=clip.bw_grade,
                    music_path=music_path,
                    whoosh_path=whoosh_path,
                    comment_bait_question=comment_bait_question,
                    add_captions=False,  # captions are added by hand in Resolve; never burned in here
                )
            elif matched is not None and cold_open:
                body_path = commentary_render.build_clip_with_cold_open(
                    filepath=job["filepath"],
                    segments=job["segments"],
                    cold_open=cold_open,
                    start=clip.start,
                    end=clip.end,
                    emphasis_words=emphasis_words,
                    pinned_line=pinned_line,
                    pinned_line_emphasis=pinned_line_emphasis,
                    preset_name=clip.caption_preset,
                    comment_bait_question=comment_bait_question,
                    add_captions=False,  # captions are added by hand in Resolve; never burned in here
                )
            elif matched is not None:
                body_path = captions.burn_captions(
                    filepath=job["filepath"],
                    segments=job["segments"],
                    start=clip.start,
                    end=clip.end,
                    emphasis_words=emphasis_words,
                    pinned_line=pinned_line,
                    pinned_line_emphasis=pinned_line_emphasis,
                    preset_name=clip.caption_preset,
                    comment_bait_question=comment_bait_question,
                    add_captions=False,  # captions are added by hand in Resolve; never burned in here
                )
            else:
                body_path = None

            if body_path is not None and commentary_close_path:
                base = os.path.splitext(os.path.basename(job["filepath"]))[0]
                caption_path = os.path.join(captions.OUTPUT_DIR, f"{base}_{clip.start:.1f}-{clip.end:.1f}_final.mp4")
                commentary_render.concat_segments([body_path, commentary_close_path], caption_path)
                # commentary_close_path itself is excluded: Resolve's
                # timeline (above) still references that exact file.
                commentary_render.cleanup_intermediates([body_path] + commentary_intermediates)
            else:
                caption_path = body_path

            results.append({
                "start": clip.start,
                "end": clip.end,
                "success": True,
                "captioned_mp4": caption_path,
            })
        except Exception as e:
            logger.exception(f"send-to-resolve failed for clip [{clip.start:.1f}-{clip.end:.1f}]")
            results.append({"start": clip.start, "end": clip.end, "success": False, "error": str(e)})

    return {"results": results}


BROLL_DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads", "broll")


def _download_broll_selection(sel: BrollSelection) -> str:
    """Fetch a human-selected B-roll pick's source file to a local path
    Resolve's scripting API can import, caching by provider+id (or a hash
    of the URL when a provider doesn't give an id) so re-sending the same
    pick across clips/sessions doesn't re-download it."""
    ext = os.path.splitext(sel.preview_url.split("?")[0])[1] or ".mp4"
    ident = sel.id or hashlib.sha1(sel.preview_url.encode()).hexdigest()[:16]
    dest = os.path.join(BROLL_DOWNLOAD_DIR, f"{sel.provider}_{ident}{ext}")
    if not os.path.exists(dest):
        broll.download_broll_file(sel.preview_url, dest)
    return dest


def _find_job_by_video_id(video_id: str) -> Optional[dict]:
    with JOBS_LOCK:
        for job in JOBS.values():
            if job.get("video_id") == video_id and job.get("status") == "done":
                return job
    return None


class SuggestBrollRequest(BaseModel):
    # When given (non-empty), B-roll placement is anchored to these beats'
    # own start/end instead of freeform query generation + fuzzy quote
    # matching — see broll.find_broll_for_beats.
    beat_plan: list[BeatPlanItem] = []


@app.post("/broll/{video_id}/{clip_index}")
def suggest_broll(video_id: str, clip_index: int, req: SuggestBrollRequest = SuggestBrollRequest()):
    """PRD §7-8 B-roll engine, on demand per clip — this isn't run
    automatically for every clip during /analyze, since each call is a
    Claude call (query generation) on top of several Pexels searches."""
    job = _find_job_by_video_id(video_id)
    if job is None:
        raise HTTPException(status_code=404, detail="video not found; run /analyze first")
    if clip_index < 0 or clip_index >= len(job["clips"]):
        raise HTTPException(status_code=404, detail="clip index out of range")

    beat_plan_dicts = [b.model_dump() for b in req.beat_plan] if req.beat_plan else None
    try:
        picks = broll.find_broll_for_clip(
            job["clips"][clip_index], segments=job.get("segments"), beat_plan=beat_plan_dicts,
        )
    except RuntimeError as e:
        # Missing PEXELS_API_KEY — an actionable 400, not a crash, since
        # B-roll is opt-in and most sessions won't have set it.
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"broll suggestion failed for {video_id} clip {clip_index}")
        raise HTTPException(status_code=502, detail=f"B-roll search failed: {e}")

    return {"picks": picks}


@app.post("/beat-plan/{video_id}/{clip_index}")
def suggest_beat_plan(video_id: str, clip_index: int):
    """beat_plan.py's editorial-structure engine, on demand per clip — one
    Claude call, same "not run automatically during /analyze" posture as
    /broll above."""
    job = _find_job_by_video_id(video_id)
    if job is None:
        raise HTTPException(status_code=404, detail="video not found; run /analyze first")
    if clip_index < 0 or clip_index >= len(job["clips"]):
        raise HTTPException(status_code=404, detail="clip index out of range")

    try:
        beats = beat_plan.generate_beat_plan(job["clips"][clip_index], job["segments"])
    except Exception as e:
        logger.exception(f"beat plan generation failed for {video_id} clip {clip_index}")
        raise HTTPException(status_code=502, detail=f"Beat plan generation failed: {e}")

    return {"beat_plan": beats}


FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

# Serves rendered captioned clips so the frontend can load/download them
# directly via the captioned_mp4 path returned from /send-to-resolve,
# rather than just a raw filesystem path.
os.makedirs(captions.OUTPUT_DIR, exist_ok=True)
app.mount("/output", StaticFiles(directory=captions.OUTPUT_DIR), name="output")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
