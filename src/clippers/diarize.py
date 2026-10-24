"""
Speaker diarization (improvement plan #3): who is talking when. faster-
whisper only gives words and times, so with a podcast or interview the crop
can't follow the speaker and the scorer can't tell who said what.

Runs pyannote.audio after transcription, caches the speaker turns next to
the video, and labels every transcript word and segment with a speaker.
Optional: needs `pip install pyannote.audio` and a Hugging Face token (HF_TOKEN
in your .env) whose account has accepted the model's terms at
https://huggingface.co/pyannote/speaker-diarization-3.1. Without either, every
function here is a no-op and the pipeline runs exactly as before.
"""

import json
import logging
import os
import subprocess
import tempfile
from typing import Optional

logger = logging.getLogger("viral_clipper.diarize")

MODEL = os.environ.get("DIARIZATION_MODEL", "pyannote/speaker-diarization-3.1")

_pipeline = None


def _token() -> Optional[str]:
    return os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")


def available() -> bool:
    if os.environ.get("DIARIZATION", "1") == "0" or not _token():
        return False
    try:
        import pyannote.audio  # noqa: F401
    except ImportError:
        return False
    return True


def _cache_path(filepath: str) -> str:
    return os.path.splitext(filepath)[0] + ".diarization.json"


def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        from pyannote.audio import Pipeline

        _pipeline = Pipeline.from_pretrained(MODEL, use_auth_token=_token())
    return _pipeline


def diarize(filepath: str) -> list[dict]:
    """Speaker turns [{"start", "end", "speaker"}] for the whole file, cached
    on disk. [] when diarization isn't set up or fails."""
    if not available():
        return []
    cache = _cache_path(filepath)
    try:
        if os.path.exists(cache) and os.path.getmtime(cache) >= os.path.getmtime(filepath):
            with open(cache, encoding="utf-8") as f:
                return json.load(f)["turns"]
    except (OSError, ValueError, KeyError):
        pass

    try:
        # pyannote wants plain audio; 16kHz mono is what its models run at.
        with tempfile.TemporaryDirectory() as tmp:
            wav = os.path.join(tmp, "audio.wav")
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", filepath, "-vn", "-ac", "1", "-ar", "16000", wav],
                check=True,
            )
            annotation = _get_pipeline()(wav)
        turns = [
            {"start": round(seg.start, 3), "end": round(seg.end, 3), "speaker": str(label)}
            for seg, _, label in annotation.itertracks(yield_label=True)
        ]
    except Exception:
        logger.exception(f"diarization failed for {filepath}; continuing without speaker labels")
        return []

    try:
        with open(cache, "w", encoding="utf-8") as f:
            json.dump({"turns": turns}, f)
    except OSError:
        logger.exception(f"could not cache diarization at {cache}")
    return turns


def _speaker_at(turns: list[dict], start: float, end: float) -> Optional[str]:
    """Speaker with the most overlap with [start, end), or the nearest turn
    when nothing overlaps (word timestamps can fall in gaps between turns)."""
    best, best_overlap = None, 0.0
    for t in turns:
        overlap = min(end, t["end"]) - max(start, t["start"])
        if overlap > best_overlap:
            best, best_overlap = t["speaker"], overlap
    if best is not None:
        return best
    mid = (start + end) / 2
    nearest = min(turns, key=lambda t: min(abs(mid - t["start"]), abs(mid - t["end"])), default=None)
    return nearest["speaker"] if nearest else None


def assign_speakers(segments: list[dict], turns: list[dict]) -> None:
    """Label each word and segment with a "speaker" in place. A segment's
    speaker is whoever says most of its words."""
    if not turns:
        return
    for seg in segments:
        counts: dict[str, float] = {}
        for w in seg.get("words", []):
            spk = _speaker_at(turns, w["start"], w["end"])
            w["speaker"] = spk
            if spk:
                counts[spk] = counts.get(spk, 0.0) + (w["end"] - w["start"])
        seg["speaker"] = max(counts, key=counts.get) if counts else _speaker_at(turns, seg["start"], seg["end"])


def speaker_count(segments: list[dict]) -> int:
    return len({s["speaker"] for s in segments if s.get("speaker")})


def turns_in_range(turns: Optional[list[dict]], start: float, end: float) -> Optional[list[dict]]:
    """Turns overlapping [start, end), clipped to it and shifted so 0 is the
    clip start (the timeline reframe.py's crop path works in). None when
    there's nothing to follow, so callers fall back to largest-face."""
    if not turns:
        return None
    out = [
        {"start": max(t["start"], start) - start, "end": min(t["end"], end) - start, "speaker": t["speaker"]}
        for t in turns
        if t["end"] > start and t["start"] < end
    ]
    if len({t["speaker"] for t in out}) < 2:
        return None
    return out


def label(segment: dict) -> str:
    """Speaker suffix for prompt lines, e.g. ' (SPEAKER_00)'; empty when unlabeled."""
    return f" ({segment['speaker']})" if segment.get("speaker") else ""
