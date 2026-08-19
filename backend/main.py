"""
FastAPI wrapper around the pipeline (PRD section 8): /analyze, /send-to-resolve,
/status/{job_id}. In-memory job state only — no database, single user, localhost.
"""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Literal, Optional

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from pipeline import download_video, use_local_file, unique_download_path, transcribe
from scoring import score_video
import resolve_integration
import captions

app = FastAPI(title="Viral Clip Finder")

EXECUTOR = ThreadPoolExecutor(max_workers=2)
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

JobStatus = Literal["queued", "downloading", "transcribing", "scoring", "done", "error"]


class AnalyzeRequest(BaseModel):
    url: Optional[str] = None
    local_file: Optional[str] = None
    title: Optional[str] = None


class ClipSelection(BaseModel):
    start: float
    end: float


class SendToResolveRequest(BaseModel):
    video_id: str
    clips: list[ClipSelection]


def _set_job(job_id: str, **fields):
    with JOBS_LOCK:
        JOBS[job_id].update(fields)


def _run_analysis(job_id: str, url: Optional[str], local_file: Optional[str] = None, title: Optional[str] = None):
    try:
        _set_job(job_id, status="downloading")
        info = use_local_file(local_file, title=title) if local_file else download_video(url)
        filepath = info["_filepath"]
        video_id = info["id"]
        title = info.get("title", video_id)

        _set_job(job_id, status="transcribing", video_id=video_id, title=title, filepath=filepath)
        segments = transcribe(filepath)

        _set_job(job_id, status="scoring", segments=segments)
        clips = score_video(segments, filepath, top_n=10)

        _set_job(job_id, status="done", clips=clips)
    except Exception as e:
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
    EXECUTOR.submit(_run_analysis, job_id, req.url, req.local_file, req.title)
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
        response["clips"] = [
            {
                "start": c["start"],
                "end": c["end"],
                "score": round(c["combined_score"], 3),
                "reason": c["reason"],
                "text": c["text"],
                "emphasis_words": c["emphasis_words"],
                "hook": c["hook"],
            }
            for c in job["clips"]
        ]
    return response


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
                    emphasis_words=matched.get("emphasis_words", []),
                )
            results.append({"start": clip.start, "end": clip.end, "success": True, "captioned_mp4": caption_path})
        except Exception as e:
            results.append({"start": clip.start, "end": clip.end, "success": False, "error": str(e)})

    return {"results": results}


def _find_job_by_video_id(video_id: str) -> Optional[dict]:
    with JOBS_LOCK:
        for job in JOBS.values():
            if job.get("video_id") == video_id and job.get("status") == "done":
                return job
    return None


FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
