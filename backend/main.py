"""
FastAPI wrapper around the pipeline (PRD section 8): /analyze, /send-to-resolve,
/status/{job_id}. In-memory job state only — no database, single user, localhost.
"""

import hashlib
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Literal, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("viral_clipper")

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from pipeline import download_video, use_local_file, unique_download_path, transcribe
from scoring import score_video
import broll
import resolve_integration
import captions
import storage

app = FastAPI(title="Viral Clip Finder")

EXECUTOR = ThreadPoolExecutor(max_workers=2)
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

JobStatus = Literal["queued", "downloading", "transcribing", "scoring", "done", "error"]


class AnalyzeRequest(BaseModel):
    url: Optional[str] = None
    local_file: Optional[str] = None
    title: Optional[str] = None
    # Church/speaker attribution — not required to find clips, but logged
    # per video for future permission outreach and claim tracing (PRD
    # pipeline step 6).
    source: Optional[str] = None


class BrollSelection(BaseModel):
    provider: str
    id: Optional[str] = None
    preview_url: str
    query: str
    offset: Optional[float] = None
    duration: Optional[float] = None


class ClipSelection(BaseModel):
    start: float
    end: float
    broll: list[BrollSelection] = []


class SendToResolveRequest(BaseModel):
    video_id: str
    clips: list[ClipSelection]


class CopyrightStatusRequest(BaseModel):
    status: Literal["unknown", "cleared"]


def _set_job(job_id: str, **fields):
    with JOBS_LOCK:
        JOBS[job_id].update(fields)


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
    }


def _run_analysis(
    job_id: str,
    url: Optional[str],
    local_file: Optional[str] = None,
    title: Optional[str] = None,
    source: Optional[str] = None,
):
    try:
        _set_job(job_id, status="downloading")
        info = use_local_file(local_file, title=title) if local_file else download_video(url)
        filepath = info["_filepath"]
        video_id = info["id"]
        title = info.get("title", video_id)

        _set_job(job_id, status="transcribing", video_id=video_id, title=title, filepath=filepath, source=source)
        segments = transcribe(filepath)

        _set_job(job_id, status="scoring", segments=segments)
        clips = score_video(segments, filepath, top_n=5)

        _set_job(job_id, status="done", clips=clips)
        # Persist hook/title/description (and timing) to disk immediately so
        # they survive a browser refresh even though JOBS is in-memory only.
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


@app.post("/analyze")
def analyze(req: AnalyzeRequest):
    if bool(req.url) == bool(req.local_file):
        raise HTTPException(status_code=400, detail="Provide exactly one of 'url' or 'local_file'")

    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "url": req.url}
    EXECUTOR.submit(_run_analysis, job_id, req.url, req.local_file, req.title, req.source)
    return {"job_id": job_id}


@app.get("/status/{job_id}")
def status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    response = {"status": job["status"]}
    if job["status"] == "error":
        response["error"] = job.get("error")
    if job["status"] == "done":
        response["video_id"] = job["video_id"]
        response["title"] = job["title"]
        response["source"] = job.get("source")
        response["copyright_status"] = job.get("copyright_status", storage.DEFAULT_COPYRIGHT_STATUS)
        response["clips"] = [_serialize_clip(c) for c in job["clips"]]
    return response


@app.get("/saved-clips")
def saved_clips():
    """All previously analyzed sessions' hook/title/description data, so the
    frontend can restore them after a browser refresh."""
    return {"sessions": storage.load_sessions()}


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

            broll_clips = []
            for sel in clip.broll:
                try:
                    local_path = _download_broll_selection(sel)
                    broll_clips.append({"filepath": local_path, "offset": sel.offset, "duration": sel.duration})
                except Exception:
                    logger.exception(f"B-roll download failed for {sel.preview_url}")

            if broll_clips:
                resolve_integration.send_clip_with_broll_to_resolve(
                    filepath=job["filepath"],
                    title=job["title"],
                    start=clip.start,
                    end=clip.end,
                    broll_clips=broll_clips,
                )
            else:
                resolve_integration.send_clip_to_resolve(
                    filepath=job["filepath"],
                    title=job["title"],
                    start=clip.start,
                    end=clip.end,
                )
            caption_path = None
            if matched is not None:
                caption_path = captions.burn_captions(
                    filepath=job["filepath"],
                    segments=job["segments"],
                    start=clip.start,
                    end=clip.end,
                    emphasis_words=emphasis_words,
                    pinned_line=pinned_line,
                    pinned_line_emphasis=pinned_line_emphasis,
                )

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


@app.post("/broll/{video_id}/{clip_index}")
def suggest_broll(video_id: str, clip_index: int):
    """PRD §7-8 B-roll engine, on demand per clip — this isn't run
    automatically for every clip during /analyze, since each call is a
    Claude call (query generation) on top of several Pexels searches."""
    job = _find_job_by_video_id(video_id)
    if job is None:
        raise HTTPException(status_code=404, detail="video not found; run /analyze first")
    if clip_index < 0 or clip_index >= len(job["clips"]):
        raise HTTPException(status_code=404, detail="clip index out of range")

    try:
        picks = broll.find_broll_for_clip(job["clips"][clip_index], segments=job.get("segments"))
    except RuntimeError as e:
        # Missing PEXELS_API_KEY — an actionable 400, not a crash, since
        # B-roll is opt-in and most sessions won't have set it.
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"broll suggestion failed for {video_id} clip {clip_index}")
        raise HTTPException(status_code=502, detail=f"B-roll search failed: {e}")

    return {"picks": picks}


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
