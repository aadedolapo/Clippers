"""
Durable, file-backed store for per-clip hook/title/description metadata so
it survives browser refreshes (and backend restarts). No database — single
JSON file, matching the rest of the app's in-memory/localhost-only scope.
"""

import json
import os
import threading
import time
from datetime import datetime
from typing import Optional

from clippers import paths

DATA_DIR = paths.DATA_DIR
STORE_PATH = os.path.join(DATA_DIR, "saved_clips.json")

_LOCK = threading.Lock()

# PRD §14: every source gets a compliance status, and it must default to a
# warning rather than a false "safe to publish" — adding commentary doesn't
# by itself resolve copyright. Only a human toggling it via
# set_copyright_status ever moves it off this default.
DEFAULT_COPYRIGHT_STATUS = "unknown"


def save_session(video_id: str, title: str, clips: list[dict], source: Optional[str] = None) -> str:
    """Persist (overwrite) the clip list for a video, keyed by video_id.
    `source` (church/speaker) is logged for future permission outreach and
    claim tracing — informational only, not required. Returns the session's
    copyright_status: carried forward from a prior save (so re-analyzing a
    video doesn't quietly reset a status a human already cleared), else
    DEFAULT_COPYRIGHT_STATUS for a brand-new video."""
    with _LOCK:
        data = _read()
        existing = data.get(video_id)
        copyright_status = existing.get("copyright_status", DEFAULT_COPYRIGHT_STATUS) if existing else DEFAULT_COPYRIGHT_STATUS
        data[video_id] = {
            "video_id": video_id,
            "title": title,
            "source": source,
            "clips": clips,
            "copyright_status": copyright_status,
            "saved_at": time.time(),
        }
        _write(data)
        return copyright_status


def set_copyright_status(video_id: str, status: str) -> dict:
    """Update just the copyright_status of an existing session. Raises
    KeyError if the video has no saved session yet."""
    with _LOCK:
        data = _read()
        if video_id not in data:
            raise KeyError(video_id)
        data[video_id]["copyright_status"] = status
        _write(data)
        return data[video_id]


def load_sessions() -> list[dict]:
    """All saved sessions, newest first."""
    with _LOCK:
        data = _read()
    return sorted(data.values(), key=lambda s: s["saved_at"], reverse=True)


def load_sessions_for_date(date_str: str) -> list[dict]:
    """Sessions whose saved_at falls on the given local calendar date
    (YYYY-MM-DD) — the daily shortlist's candidate pool (retention upgrade
    plan phase 4). Uses the machine's local timezone, matching a single-user
    localhost tool's "today" rather than UTC's."""
    return [s for s in load_sessions() if datetime.fromtimestamp(s["saved_at"]).date().isoformat() == date_str]


def _read() -> dict:
    if not os.path.exists(STORE_PATH):
        return {}
    with open(STORE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _write(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_path = STORE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, STORE_PATH)
