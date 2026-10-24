"""
FastAPI wrapper around the pipeline: /analyze, /send-to-resolve,
/status/{job_id}. Job state lives in memory and is mirrored to one JSON file
per job (jobs_store.py), so finished jobs survive a restart. Single user,
localhost.
"""

import hashlib
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from clippers import (
    beat_plan,
    broll,
    captions,
    commentary,
    commentary_render,
    diarize,
    jobs_store,
    llm_scoring,
    music,
    paths,
    performance,
    resolve_integration,
    sfx,
    shots,
    storage,
)
from clippers.pipeline import download_video, transcribe, unique_download_path, use_local_file
from clippers.scoring import score_video

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("viral_clipper")

EXECUTOR = ThreadPoolExecutor(max_workers=2)
# Background helpers a job waits on (shot detection), kept separate so they
# can't deadlock behind the analysis jobs that submitted them.
SIDE_EXECUTOR = ThreadPoolExecutor(max_workers=2)
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def _submit_job(local_file: Optional[str] = None, url: Optional[str] = None,
                title: Optional[str] = None, source: Optional[str] = None,
                long_form: bool = False) -> str:
    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "url": url, "local_file": local_file,
                        "requested_title": title, "source": source, "long_form": long_form,
                        "created_at": time.time()}
    _persist_job(job_id)
    EXECUTOR.submit(_run_analysis, job_id, url, local_file, title, source, long_form)
    return job_id


@asynccontextmanager
async def lifespan(_app):
    # Finished jobs come back from disk; ones cut off mid-run come back as
    # errors that can be retried (see jobs_store.load_all).
    restored = jobs_store.load_all()
    with JOBS_LOCK:
        JOBS.update(restored)
    if restored:
        logger.info(f"restored {len(restored)} job(s) from {jobs_store.JOBS_DIR}")
    yield


app = FastAPI(title="Viral Clip Finder", lifespan=lifespan)

JobStatus = Literal["queued", "downloading", "transcribing", "scoring", "done", "error"]


class AnalyzeRequest(BaseModel):
    url: Optional[str] = None
    local_file: Optional[str] = None
    title: Optional[str] = None
    # Source/speaker attribution — not required to find clips, but logged
    # per video for future permission outreach and claim tracing.
    source: Optional[str] = None
    # Look for longer story clips (llm_scoring.LONG, 45-90s by default)
    # instead of Shorts-length ones.
    long_form: bool = False


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
    # on when the clip has one: the checkbox turns it off, not on.
    use_cold_open: bool = True
    # Add a spoken outro — opt-in,
    # since it's a new editorial layer, not a bug-fix default like
    # use_cold_open. Intro is deliberately not offered: anything before the
    # hook hurts retention, so only the outro is ever wired in.
    with_commentary: bool = False
    # A short on-screen comment-bait question after the clip ends — text
    # only, never spoken, so it never triggers a second ElevenLabs TTS call
    # even when with_commentary is also on (both reuse the same
    # commentary.py call; see send_to_resolve).
    comment_bait: bool = False
    # "Minimal Faith" style — the new
    # default caption preset. use_cold_open is silently skipped for this
    # preset: it renders via its own effects pipeline (zoom/grade/music),
    # which doesn't have a cold-open crossfade path built for it yet.
    caption_preset: str = captions.MINIMAL_FAITH_PRESET
    zoom_opener: bool = True
    bw_grade: bool = False
    music_bed: bool = False
    music_mood: Literal["slow", "cinematic"] = "slow"
    # Burn captions into the output/ mp4. Off by
    # default; the Resolve timeline never gets burned-in captions either
    # way, so native subtitles there still work.
    burn_captions: bool = False
    # Output canvas for the output/ mp4. The Resolve timeline stays 9:16.
    aspect: Literal["9:16", "4:5", "1:1"] = "9:16"
    # Face-following crop (reframe.py) instead of a fixed center crop.
    follow_speaker: bool = True


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
    # Persist on status changes and final results only: a done job carries
    # the full word-level transcript.
    if "status" in fields or "copyright_status" in fields:
        _persist_job(job_id)


def _persist_job(job_id: str):
    with JOBS_LOCK:
        snapshot = dict(JOBS[job_id])
    try:
        jobs_store.save_job(job_id, snapshot)
    except Exception:
        logger.exception(f"failed to persist job {job_id}")


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
        "visual": c.get("visual"),
        "pre_shot_snap": c.get("pre_shot_snap"),
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
    long_form: bool = False,
):
    try:
        _set_job(job_id, status="downloading")
        info = use_local_file(local_file, title=title) if local_file else download_video(url)
        filepath = info["_filepath"]
        video_id = info["id"]
        title = info.get("title", video_id)

        _set_job(job_id, status="transcribing", video_id=video_id, title=title, filepath=filepath, source=source)
        # Shot detection only needs the video, so it runs alongside
        # transcription instead of after it.
        shots_future = SIDE_EXECUTOR.submit(shots.detect_shots, filepath)
        segments = transcribe(filepath)

        speaker_turns = []
        if diarize.available():
            speaker_turns = diarize.diarize(filepath)
            diarize.assign_speakers(segments, speaker_turns)
        shot_cuts = shots_future.result()

        lengths = llm_scoring.LONG if long_form else llm_scoring.SHORT
        _set_job(job_id, status="scoring", segments=segments, shot_cuts=shot_cuts, speaker_turns=speaker_turns)
        # Top 3 per video — 8 was producing more candidates than were
        # actually worth reviewing per video.
        clips, rejected = score_video(segments, filepath, top_n=3, shot_cuts=shot_cuts, lengths=lengths)

        _set_job(job_id, status="done", clips=clips, rejected=rejected)
        # Also save hook/title/description (and timing) to the session store,
        # which the shortlist and performance log read across videos.
        copyright_status = storage.save_session(
            video_id, title, [_serialize_clip(c) for c in clips], source=source
        )
        _set_job(job_id, copyright_status=copyright_status)
    except Exception as e:
        logger.exception(f"analyze job {job_id} failed")
        _set_job(job_id, status="error", error=str(e))


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


def _require_api_key():
    """Scoring needs Claude, so refuse up front rather than after the
    download and transcription, which can take minutes."""
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise HTTPException(status_code=400, detail=(
            f"ANTHROPIC_API_KEY isn't set. Add it to {paths.ENV_FILE}, then restart `clippers serve`."
        ))


@app.post("/analyze")
def analyze(req: AnalyzeRequest):
    if bool(req.url) == bool(req.local_file):
        raise HTTPException(status_code=400, detail="Provide exactly one of 'url' or 'local_file'")
    _require_api_key()

    job_id = _submit_job(local_file=req.local_file, url=req.url, title=req.title,
                         source=req.source, long_form=req.long_form)
    return {"job_id": job_id}


@app.get("/jobs")
def list_jobs():
    """Every job, including ones restored from disk, newest last."""
    with JOBS_LOCK:
        jobs = [
            {"job_id": jid, "status": j["status"], "title": j.get("title") or j.get("requested_title") or os.path.basename(j.get("local_file") or j.get("url") or ""),
             "video_id": j.get("video_id"), "error": j.get("error"),
             "interrupted": j.get("interrupted", False), "created_at": j.get("created_at"),
             "retried_as": j.get("retried_as"), "dismissed": j.get("dismissed", False)}
            for jid, j in JOBS.items()
        ]
    jobs.sort(key=lambda j: j["created_at"] or 0)
    return {"jobs": jobs}


def _failed_job(job_id: str) -> dict:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job["status"] != "error":
        raise HTTPException(status_code=400, detail="only failed jobs can be retried or dismissed")
    return job


@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: str):
    """Re-run a failed job (typically one interrupted by a restart) with the
    same input. Returns the new job's id; the failed one stays in the list,
    marked with retried_as so the main page stops offering it."""
    job = _failed_job(job_id)
    if job.get("retried_as"):
        raise HTTPException(status_code=400, detail="job was already retried")
    _require_api_key()
    # Reuse the copy already in downloads/ when the first run got that far,
    # so the retry keeps the same video_id instead of making a second copy.
    copied = job.get("filepath") if job.get("filepath") and os.path.exists(job["filepath"]) else None
    new_id = _submit_job(local_file=copied or job.get("local_file"), url=None if copied else job.get("url"),
                         title=job.get("requested_title"), source=job.get("source"),
                         long_form=job.get("long_form", False))
    with JOBS_LOCK:
        job["retried_as"] = new_id
    _persist_job(job_id)
    return {"job_id": new_id}


@app.post("/jobs/{job_id}/dismiss")
def dismiss_job(job_id: str):
    """Hide a failed job from the main page without retrying it."""
    job = _failed_job(job_id)
    with JOBS_LOCK:
        job["dismissed"] = True
    _persist_job(job_id)
    return {"ok": True}


@app.get("/status/{job_id}")
def status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    response = {"status": job["status"], "title": job.get("title")}
    if job["status"] == "error":
        response["error"] = job.get("error")
        response["interrupted"] = job.get("interrupted", False)
    if job["status"] == "done":
        response["video_id"] = job["video_id"]
        response["title"] = job["title"]
        response["source"] = job.get("source")
        response["copyright_status"] = job.get("copyright_status", storage.DEFAULT_COPYRIGHT_STATUS)
        response["clips"] = [_serialize_clip(c) for c in job["clips"]]
        response["rejected"] = [_serialize_rejected(c) for c in job.get("rejected", [])]
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
    """Compliance layer: a human explicitly marking a source cleared
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
            job_id = next((jid for jid, j in JOBS.items() if j is job), None)
        if job_id is not None:
            _persist_job(job_id)

    return {"video_id": video_id, "copyright_status": record["copyright_status"]}


@app.post("/send-to-resolve")
def send_to_resolve(req: SendToResolveRequest):
    """Put each clip on the Resolve timeline and render its ready-to-post
    mp4 into output/. The mp4 doesn't need Resolve, so when Resolve can't be
    reached (free version without external scripting, not open, or not
    installed) the clip is still rendered for finishing in another editor,
    and the result carries resolve_error instead of failing."""
    job = _find_job_by_video_id(req.video_id)
    if job is None:
        raise HTTPException(status_code=404, detail="video not found; run /analyze first")

    results = []
    # Set on the first clip that finds Resolve unreachable, so the rest of
    # this request skips it instead of waiting on the connection again.
    resolve_down: Optional[str] = None
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

            # Spoken outro (phase 6): rendered once up front so the same
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

            resolve_error = resolve_down
            if resolve_error is None:
                try:
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
                except resolve_integration.ResolveUnavailable as e:
                    resolve_down = resolve_error = str(e)
                    logger.warning(f"Resolve unavailable, rendering mp4 only: {e}")
                except Exception as e:
                    resolve_error = str(e)
                    logger.exception(f"Resolve step failed for clip [{clip.start:.1f}-{clip.end:.1f}]; rendering mp4 anyway")

            caption_path = None
            render_opts = {
                "add_captions": clip.burn_captions,
                "aspect": clip.aspect,
                "follow_speaker": clip.follow_speaker,
            }

            def turns_for(a: float, b: float):
                return diarize.turns_in_range(job.get("speaker_turns"), a, b)

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
                    speaker_turns=turns_for(clip.start, clip.end),
                    **render_opts,
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
                    speaker_turns_for=turns_for,
                    **render_opts,
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
                    speaker_turns=turns_for(clip.start, clip.end),
                    **render_opts,
                )
            else:
                body_path = None

            if body_path is not None and commentary_close_path:
                base = os.path.splitext(os.path.basename(job["filepath"]))[0]
                suffix = "" if clip.aspect == captions.DEFAULT_ASPECT else "_" + clip.aspect.replace(":", "x")
                caption_path = os.path.join(captions.OUTPUT_DIR, f"{base}_{clip.start:.1f}-{clip.end:.1f}_final{suffix}.mp4")
                commentary_render.concat_segments([body_path, commentary_close_path], caption_path)
                # commentary_close_path itself is excluded: Resolve's
                # timeline (above) still references that exact file.
                commentary_render.cleanup_intermediates([body_path] + commentary_intermediates)
            else:
                caption_path = body_path

            if resolve_error and not caption_path:
                raise RuntimeError(resolve_error)
            results.append({
                "start": clip.start,
                "end": clip.end,
                "success": True,
                "captioned_mp4": os.path.normpath(caption_path) if caption_path else None,
                "in_resolve": resolve_error is None,
                "resolve_error": resolve_error,
                # B-roll only goes on the Resolve timeline, never into the mp4.
                "broll_skipped": bool(resolve_error and broll_clips),
            })
        except Exception as e:
            logger.exception(f"send-to-resolve failed for clip [{clip.start:.1f}-{clip.end:.1f}]")
            results.append({"start": clip.start, "end": clip.end, "success": False, "error": str(e)})

    return {"results": results}


BROLL_DOWNLOAD_DIR = paths.BROLL_DOWNLOAD_DIR


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
    """Newest finished job for this video. Falls back to disk for a job this
    process never loaded (e.g. written by another server instance)."""
    with JOBS_LOCK:
        done = [j for j in JOBS.values() if j.get("video_id") == video_id and j.get("status") == "done"]
        if done:
            return max(done, key=lambda j: j.get("created_at") or 0)
    found = jobs_store.find_done_job_by_video_id(video_id)
    if found is None:
        return None
    job_id, job = found
    with JOBS_LOCK:
        return JOBS.setdefault(job_id, job)


class SuggestBrollRequest(BaseModel):
    # When given (non-empty), B-roll placement is anchored to these beats'
    # own start/end instead of freeform query generation + fuzzy quote
    # matching — see broll.find_broll_for_beats.
    beat_plan: list[BeatPlanItem] = []


@app.post("/broll/{video_id}/{clip_index}")
def suggest_broll(video_id: str, clip_index: int, req: SuggestBrollRequest = SuggestBrollRequest()):
    """B-roll engine, on demand per clip — this isn't run
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


FRONTEND_DIR = paths.STATIC_DIR


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

# Serves rendered captioned clips so the frontend can load/download them
# directly via the captioned_mp4 path returned from /send-to-resolve,
# rather than just a raw filesystem path.
os.makedirs(captions.OUTPUT_DIR, exist_ok=True)
app.mount("/output", StaticFiles(directory=captions.OUTPUT_DIR), name="output")

