"""
Durable job state: one JSON file per analysis job under backend/data/jobs/,
so a finished job's segments and candidate clips survive a server restart.
Without this, Send to Resolve, B-roll and beat plans all 404 for anything
analyzed before the last restart, since main.py looks jobs up in memory.
"""

import json
import os
import threading
from typing import Optional

JOBS_DIR = os.path.join(os.path.dirname(__file__), "data", "jobs")

# Statuses a job only passes through while a worker thread owns it. A job
# found on disk in one of these at startup was cut off by the restart.
RUNNING_STATUSES = {"queued", "downloading", "transcribing", "scoring"}
INTERRUPTED_ERROR = "Interrupted by a server restart. Retry to run it again."

_LOCK = threading.Lock()


def _path(job_id: str) -> str:
    return os.path.join(JOBS_DIR, f"{job_id}.json")


def _json_default(o):
    # Scores computed with numpy can leak np.float32/np.int64 into clip dicts.
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def save_job(job_id: str, job: dict) -> None:
    """Write a snapshot of the job (the caller passes a copy, so the worker
    thread can keep mutating its own dict). Atomic via tmp file + rename."""
    os.makedirs(JOBS_DIR, exist_ok=True)
    tmp_path = _path(job_id) + ".tmp"
    with _LOCK:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({**job, "job_id": job_id}, f, default=_json_default)
        os.replace(tmp_path, _path(job_id))


def load_job(job_id: str) -> Optional[dict]:
    try:
        with open(_path(job_id), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def load_all() -> dict[str, dict]:
    """Every job on disk, keyed by job_id. Jobs that were still running when
    the server stopped come back as status=error with interrupted=True (and
    are rewritten that way), so the UI can offer a retry instead of showing
    them as stuck forever."""
    if not os.path.isdir(JOBS_DIR):
        return {}
    jobs = {}
    for name in os.listdir(JOBS_DIR):
        if not name.endswith(".json"):
            continue
        job_id = name[: -len(".json")]
        job = load_job(job_id)
        if job is None:
            continue
        job.pop("job_id", None)
        if job.get("status") in RUNNING_STATUSES:
            job["status"] = "error"
            job["error"] = INTERRUPTED_ERROR
            job["interrupted"] = True
            save_job(job_id, job)
        jobs[job_id] = job
    return jobs


def find_done_job_by_video_id(video_id: str) -> Optional[tuple[str, dict]]:
    """Newest finished job for a video, read straight from disk."""
    best = None
    for job_id, job in load_all().items():
        if job.get("video_id") != video_id or job.get("status") != "done":
            continue
        if best is None or job.get("created_at", 0) > best[1].get("created_at", 0):
            best = (job_id, job)
    return best
