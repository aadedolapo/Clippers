"""
Visual signal in scoring (improvement plan #5). The text scorer only sees
the transcript and loudness, so it can't tell a strong visual moment
(reaction, gesture, laughter, a crowd) from a static talking head, or spot
a first frame that's the back of someone's head.

After the text pass, each shortlisted candidate gets a second "visual
rerank" call: a handful of frames (first frame, hook moment, middle,
payoff, last) plus the transcript, scored by Claude. Only the shortlist is
sent, never every window, so this costs one call per candidate.
"""

import base64
import json
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logger = logging.getLogger("viral_clipper.visual_rerank")

MODEL = os.environ.get("CLAUDE_VISUAL_MODEL", "claude-opus-5-5")
ENABLED = os.environ.get("VISUAL_RERANK", "1") != "0"
CONCURRENCY = int(os.environ.get("VISUAL_RERANK_CONCURRENCY", "4"))
FRAME_WIDTH = 512

# Neutral visual score for candidates outside the shortlist, so they're
# neither rewarded nor punished for not being looked at.
NEUTRAL_SCORE = 0.5

SYSTEM_PROMPT = """You judge the visuals of candidate short-form clips (YouTube \
Shorts / Reels / TikTok) cut from a longer video. You get a few frames from the \
clip, in order, each labeled with its time and role, plus the clip's transcript.

Score:
- first_frame_strength (0-10): would the FIRST frame alone stop someone \
scrolling? High for a clear, well-lit face looking toward camera, a strong \
expression or gesture, an interesting scene. Low for the back of a head, \
a blurry or mid-transition frame, an empty stage, a slide, black.
- visual_energy (0-10): how visually alive the clip is across the frames: \
movement, gestures, expressions, crowd reactions, scene variety. A static \
talking head with no change scores around 3.
- face_not_visible: true if no face is clearly visible in most frames.
- slide_or_screen: true if the frames are mostly a slide, screen recording \
or text graphic.
- black_frame: true if the first frame is black or nearly black.
- note: one short sentence on the strongest visual problem or strength."""

SCHEMA = {
    "type": "object",
    "properties": {
        "first_frame_strength": {"type": "integer"},
        "visual_energy": {"type": "integer"},
        "face_not_visible": {"type": "boolean"},
        "slide_or_screen": {"type": "boolean"},
        "black_frame": {"type": "boolean"},
        "note": {"type": "string"},
    },
    "required": ["first_frame_strength", "visual_energy", "face_not_visible",
                 "slide_or_screen", "black_frame", "note"],
    "additionalProperties": False,
}


def _grab_frame(filepath: str, t: float) -> Optional[bytes]:
    out = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", filepath, "-frames:v", "1",
         "-vf", f"scale={FRAME_WIDTH}:-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4", "pipe:1"],
        capture_output=True,
    )
    return out.stdout or None


def _hook_time(clip: dict) -> Optional[float]:
    """Where the strongest line plays: the cold open's start, if the clip
    has one. None otherwise (frame_plan then samples ~2s in)."""
    if clip.get("cold_open"):
        return clip["cold_open"]["start"]
    return None


def frame_plan(clip: dict) -> list[tuple[str, float]]:
    """(role, source time) for the 4-6 frames sent per candidate."""
    start, end = clip["start"], clip["end"]
    dur = end - start
    plan = [("first frame", start + 0.05)]
    hook = _hook_time(clip)
    if hook is not None:
        plan.append(("hook moment (cold open)", hook + 0.5))
    else:
        plan.append(("hook moment", start + min(2.0, dur * 0.1)))
    plan += [("middle", start + dur * 0.5), ("payoff", start + dur * 0.8), ("last frame", end - 0.2)]
    return plan


def visual_score(result: dict) -> float:
    """0-1 term for scoring.WEIGHTS["visual"]. A black first frame zeroes
    it; a missing face or a slide caps it, since either almost always
    loses the scroll on a talking-head channel."""
    if result.get("black_frame"):
        return 0.0
    score = (0.6 * result["first_frame_strength"] + 0.4 * result["visual_energy"]) / 10.0
    if result.get("face_not_visible") or result.get("slide_or_screen"):
        score = min(score, 0.3)
    return max(0.0, min(1.0, score))


def rerank_one(client: Anthropic, filepath: str, clip: dict) -> Optional[dict]:
    content = []
    for role, t in frame_plan(clip):
        jpg = _grab_frame(filepath, t)
        if not jpg:
            continue
        content.append({"type": "text", "text": f"{role} ({t - clip['start']:.1f}s into the clip):"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(jpg).decode()}})
    if not content:
        return None
    content.append({"type": "text", "text": f"Transcript:\n{clip['text']}"})

    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        logger.warning(f"visual rerank refused for [{clip['start']:.1f}-{clip['end']:.1f}]")
        return None
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        return None
    result = json.loads(text)
    result["score"] = visual_score(result)
    return result


def rerank(filepath: str, clips: list[dict]) -> None:
    """Attach clip["visual"] (the model's result plus "score") to each clip,
    in place and in parallel. Failures leave a clip without "visual"; the
    caller treats that as NEUTRAL_SCORE."""
    if not ENABLED or not clips:
        return
    client = Anthropic()

    def run(clip):
        try:
            return clip, rerank_one(client, filepath, clip)
        except Exception:
            logger.exception(f"visual rerank failed for [{clip['start']:.1f}-{clip['end']:.1f}]")
            return clip, None

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for clip, result in pool.map(run, clips):
            if result is not None:
                clip["visual"] = result
