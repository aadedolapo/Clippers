"""
Watch folder: polls a directory (4K Video Downloader's output folder) and
hands each newly finished video to a callback exactly once.

Polling instead of filesystem events so there's no extra dependency, and so
a file still being written is easy to detect: it's only "finished" once its
size has stopped changing across consecutive scans.
"""

import json
import logging
import os
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("viral_clipper.watcher")

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
POLL_SECONDS = 10
# Consecutive scans a file's size must stay the same before it's picked up.
STABLE_SCANS = 3
STATE_PATH = os.path.join(os.path.dirname(__file__), "data", "watch_state.json")


def _load_seen() -> Optional[set[str]]:
    try:
        with open(STATE_PATH) as f:
            return set(json.load(f)["seen"])
    except FileNotFoundError:
        return None


def _save_seen(seen: set[str]) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"seen": sorted(seen)}, f, indent=2)
    os.replace(tmp, STATE_PATH)


def _list_videos(watch_dir: str) -> dict[str, int]:
    out = {}
    for name in os.listdir(watch_dir):
        if name.startswith("."):
            continue
        if os.path.splitext(name)[1].lower() not in VIDEO_EXTS:
            continue
        path = os.path.join(watch_dir, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size > 0:
            out[path] = size
    return out


def _file_key(path: str) -> str:
    # Path + mtime, so a re-download that overwrites the same filename still
    # counts as new, but a restart doesn't reprocess an untouched file.
    return f"{path}|{int(os.path.getmtime(path))}"


def start_watcher(watch_dir: str, on_new_video: Callable[[str], None]) -> threading.Thread:
    """Start a daemon thread watching `watch_dir`. On the very first run (no
    state file yet) every video already in the folder is marked as seen, so
    pointing this at a folder full of old downloads doesn't kick off a
    batch of jobs."""
    watch_dir = os.path.abspath(os.path.expanduser(watch_dir))
    if not os.path.isdir(watch_dir):
        raise FileNotFoundError(f"Watch folder not found: {watch_dir}")

    seen = _load_seen()
    if seen is None:
        seen = {_file_key(p) for p in _list_videos(watch_dir)}
        _save_seen(seen)
        logger.info(f"watch folder first run: ignoring {len(seen)} existing video(s) in {watch_dir}")

    def loop():
        sizes: dict[str, tuple[int, int]] = {}  # path -> (size, scans unchanged)
        while True:
            try:
                current = _list_videos(watch_dir)
                for path, size in current.items():
                    prev_size, stable = sizes.get(path, (-1, 0))
                    stable = stable + 1 if size == prev_size else 0
                    sizes[path] = (size, stable)
                    if stable < STABLE_SCANS:
                        continue
                    key = _file_key(path)
                    if key in seen:
                        continue
                    seen.add(key)
                    _save_seen(seen)
                    logger.info(f"watch folder: new video {path}")
                    try:
                        on_new_video(path)
                    except Exception:
                        logger.exception(f"watch folder: failed to start job for {path}")
                for path in list(sizes):
                    if path not in current:
                        del sizes[path]
            except Exception:
                logger.exception("watch folder scan failed")
            time.sleep(POLL_SECONDS)

    thread = threading.Thread(target=loop, name="watch-folder", daemon=True)
    thread.start()
    logger.info(f"watching {watch_dir} for new videos")
    return thread
