"""
Music bed selection (retention upgrade plan phase 7): pick a royalty-free
track by mood from the user's own local library, avoiding recent repeats.
"""

import json
import os
import random
import threading
from typing import Optional

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
HISTORY_PATH = os.path.join(DATA_DIR, "music_history.json")

HISTORY_SIZE = 5
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac")

_LOCK = threading.Lock()


def _music_dirs() -> dict[str, str]:
    """Parse MUSIC_DIRS ("mood=path,mood=path") from the environment into a
    {mood: expanded_dir} map. Missing/malformed entries are skipped rather
    than raising, since a bad .env value shouldn't crash analysis — it
    should just mean no track gets picked for that mood."""
    raw = os.environ.get("MUSIC_DIRS", "")
    dirs = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        mood, path = entry.split("=", 1)
        dirs[mood.strip()] = os.path.expanduser(path.strip())
    return dirs


def _read_history() -> list[str]:
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH, encoding="utf-8") as f:
        return json.load(f)


def _write_history(history: list[str]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_path = HISTORY_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    os.replace(tmp_path, HISTORY_PATH)


def pick_track(mood: str) -> Optional[str]:
    """Pick a random track for `mood`, avoiding the last HISTORY_SIZE tracks
    used (falls back to the full pool if avoiding history would leave
    nothing to pick, e.g. a mood folder with only 1-4 tracks in it). Returns
    None if the mood is unknown or its folder has no audio files —
    callers should skip the music bed silently in that case."""
    mood_dir = _music_dirs().get(mood)
    if not mood_dir or not os.path.isdir(mood_dir):
        return None

    tracks = [
        os.path.join(mood_dir, f)
        for f in os.listdir(mood_dir)
        if f.lower().endswith(AUDIO_EXTS)
    ]
    if not tracks:
        return None

    with _LOCK:
        history = _read_history()
        candidates = [t for t in tracks if t not in history] or tracks
        choice = random.choice(candidates)
        history = ([choice] + [h for h in history if h != choice])[:HISTORY_SIZE]
        _write_history(history)

    return choice
