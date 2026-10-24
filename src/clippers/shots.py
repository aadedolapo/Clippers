"""
Shot-boundary detection and snapping. Clip boundaries
otherwise only snap to transcript segments, so a clip can open a frame or
two before a hard cut: a flash of the previous shot as the very first
frame, which is the frame that decides whether someone swipes away.

PySceneDetect runs once per video; the cut list is cached as JSON next to
the video in downloads/, so re-analyzing the same file is free.
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger("viral_clipper.shots")

# A cut this close to the chosen start (either side) pulls the start onto it.
START_SNAP_WINDOW = 0.5
# When the speaker is already talking before a cut just after the start,
# look this far back for an earlier cut to open on instead.
START_BACKOFF_WINDOW = 1.5
# The end must not land within this many seconds after a cut (a few frames
# of a new shot as the last thing on screen).
END_GUARD = 0.15
# Word timestamps are approximate; treat speech starting this close to a
# cut as starting on it.
WORD_SLOP = 0.05


def _cache_path(filepath: str) -> str:
    return os.path.splitext(filepath)[0] + ".shots.json"


def detect_shots(filepath: str) -> list[float]:
    """Hard-cut timestamps (seconds) for the whole video, cached on disk.
    Returns [] if detection fails: shot snapping is an improvement, never a
    reason to fail an analysis job."""
    cache = _cache_path(filepath)
    try:
        if os.path.exists(cache) and os.path.getmtime(cache) >= os.path.getmtime(filepath):
            with open(cache, encoding="utf-8") as f:
                return json.load(f)["cuts"]
    except (OSError, ValueError, KeyError):
        pass

    try:
        from scenedetect import ContentDetector, detect
        scenes = detect(filepath, ContentDetector())
    except Exception:
        logger.exception(f"shot detection failed for {filepath}; continuing without shot snapping")
        return []

    # Each scene is (start, end); a cut is every scene start after the first.
    cuts = [round(s[0].get_seconds(), 3) for s in scenes[1:]]
    try:
        with open(cache, "w", encoding="utf-8") as f:
            json.dump({"cuts": cuts}, f)
    except OSError:
        logger.exception(f"could not cache shot cuts at {cache}")
    return cuts


def _speech_between(words: list[dict], a: float, b: float) -> bool:
    return any(w["start"] < b and w["end"] > a for w in words)


def snap_to_shots(
    start: float,
    end: float,
    cuts: list[float],
    words: Optional[list[dict]] = None,
) -> tuple[float, float]:
    """Move [start, end) so it doesn't open on, or close on, a sliver of a
    shot. `words` are whisper word dicts ({"start","end"}) used to avoid
    clipping speech; without them a forward snap is always allowed.

    Start, in order of preference:
      1. A cut up to START_SNAP_WINDOW before the start: open on the cut.
      2. A cut up to START_SNAP_WINDOW after the start, with no speech yet
         before it: open on the cut (only silence is trimmed).
      3. Same, but speech has started: open on the previous cut instead if
         one is within START_BACKOFF_WINDOW with no speech in between.
      4. Otherwise open on the cut anyway; losing a fraction of the first
         word is better than a flash frame, and edited footage nearly always
         cuts on a word boundary.
    End: if a cut falls in the last END_GUARD seconds, end on that cut."""
    if not cuts:
        return start, end
    words = words or []

    before = [c for c in cuts if start - START_SNAP_WINDOW <= c <= start]
    after = [c for c in cuts if start < c <= start + START_SNAP_WINDOW]
    if before:
        start = max(before)
    elif after:
        cut = min(after)
        if not _speech_between(words, start, cut - WORD_SLOP):
            start = cut
        else:
            earlier = [c for c in cuts if start - START_BACKOFF_WINDOW <= c < start]
            if earlier and not _speech_between(words, max(earlier), start):
                start = max(earlier)
            else:
                start = cut

    tail = [c for c in cuts if end - END_GUARD < c < end]
    if tail and min(tail) > start:
        end = min(tail)
    return start, end


def words_in(segments: list[dict], start: float, end: float) -> list[dict]:
    return [w for s in segments for w in s.get("words", []) if w["end"] > start and w["start"] < end]


def snap_clip(clip: dict, cuts: list[float], segments: list[dict]) -> dict:
    """Apply snap_to_shots to a candidate clip (and its cold open) in place.
    Records the original range so a human can see what moved."""
    if not cuts:
        return clip
    margin = START_BACKOFF_WINDOW + 1.0
    words = words_in(segments, clip["start"] - margin, clip["end"] + margin)
    new_start, new_end = snap_to_shots(clip["start"], clip["end"], cuts, words)
    if (new_start, new_end) != (clip["start"], clip["end"]):
        clip.setdefault("pre_shot_snap", {"start": clip["start"], "end": clip["end"]})
        clip["start"], clip["end"] = new_start, new_end
    co = clip.get("cold_open")
    if co:
        co_words = words_in(segments, co["start"] - margin, co["end"] + margin)
        co["start"], co["end"] = snap_to_shots(co["start"], co["end"], cuts, co_words)
    return clip


def cuts_between(cuts: list[float], start: float, end: float) -> list[float]:
    return [c for c in cuts if start <= c < end]


if __name__ == "__main__":
    import sys

    found = detect_shots(sys.argv[1])
    print(f"{len(found)} cuts")
    for c in found[:50]:
        print(f"  {c:8.2f}s")
