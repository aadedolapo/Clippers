"""
Beat-plan engine: for one already-selected clip, break its transcript into an
ordered sequence of editorial beats (speaker statement, punch-in, B-roll, back
to speaker, B-roll+sound, payoff) instead of leaving the clip as one flat,
unstructured range. Downstream, this drives precise B-roll placement
(broll.py's beat_plan param) and, later, punch-in overlays.

Called on demand per clip (like broll.py's suggestion engine), never
automatically during /analyze — each call is one Claude call, and a beat plan
is only ever useful on a clip a human has already chosen to work with.
"""

import json
import os
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

MODEL = os.environ.get("CLAUDE_BEAT_PLAN_MODEL", "claude-sonnet-5")

BEAT_TYPES = ["statement", "punch_in", "broll", "back_to_speaker", "broll_sound", "payoff"]

# Duration bounds per beat type, seconds — only enforced for types where a
# wrong-size beat would actually break downstream rendering (punch_in feeds
# a fixed-zoom overlay window, broll/broll_sound feed a B-roll cutaway sized
# like resolve_integration.py's DEFAULT_BROLL_CUTAWAY_SECONDS=2.5). Other
# types (statement, back_to_speaker, payoff) just need to be inside the
# clip's own bounds — no fixed shape to enforce.
BEAT_DURATION_BOUNDS = {
    "punch_in": (0.5, 4.0),
    "broll": (1.5, 6.0),
    "broll_sound": (1.5, 6.0),
}

SYSTEM_PROMPT = """You break a short-form video clip's transcript into an ordered sequence of \
editorial beats — the shot-by-shot story structure an editor would cut to, not just a \
flat clip. The goal is an arc (statement -> emotion -> visual proof -> escalation -> payoff), \
never a flat repetition of the same beat type back to back.

Beat types, in the order they'd typically appear (not every clip needs all of them). Segment \
durations vary, so use these as target durations, not exact ones — usually ONE segment gets \
you a punch_in-sized beat, and TWO OR THREE consecutive segments get you a broll-sized one:
- "statement" (no fixed target): the speaker's face, delivering the clip's strongest opening line.
- "punch_in" (target ~1-3 seconds, typically exactly one segment): a tighter crop / angle \
change on the speaker for emphasis, timed to one specific word or short phrase landing hard.
- "broll" (target ~2-5 seconds, typically 2-3 consecutive segments): B-roll footage \
illustrating whatever was just said.
- "back_to_speaker" (no fixed target): cut back to the speaker's normal framing.
- "broll_sound" (target ~2-5 seconds, typically 2-3 consecutive segments): B-roll again, \
paired with a sound-design moment (whoosh/riser/impact).
- "payoff" (no fixed target): the speaker's face delivering the clip's resolution/strongest \
closing idea.

Given a clip's transcript as numbered segments (e.g. "[3] 12.4s-14.1s: text"), return 3-6 \
beats as an ordered, non-overlapping sequence covering the clip from its first segment to \
its last. Always include "statement" as the first beat and "payoff" as the last one. Only \
include the other types where they genuinely fit this specific transcript and where you can \
hit their target duration with the segments available — never force a punch_in or B-roll beat \
onto a clip that's just one continuous thought with nothing to cut away to, and never stretch \
one to cover far more segments than its target duration needs.

For each beat, return:
- "beat_type": one of the types above
- "start_segment" / "end_segment": inclusive segment-index range (0-based) this beat covers
- "note": for "broll"/"broll_sound" only — a short (3-8 word) concrete visual description \
of what footage would illustrate this beat (same concreteness bar as stock-footage search: \
name a literal filmable subject, not an abstract word). Omit for other beat types.
- "emphasis_word": for "punch_in" only — the exact word or short phrase (copied verbatim \
from this beat's segment text) the punch-in should land on. Omit for other beat types.

Respond with a single JSON array of beat objects, no prose, no markdown fences."""


def _strip_json_fence(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw


def _clip_segments(segments: list[dict], clip_start: float, clip_end: float) -> list[dict]:
    return [s for s in segments if s["start"] < clip_end and s["end"] > clip_start]


def generate_beat_plan(clip: dict, segments: list[dict]) -> Optional[list[dict]]:
    """One Claude call for one already-selected clip. Returns an ordered list
    of {"beat_type", "start", "end", "note", "emphasis_word"} (start/end are
    absolute source-media seconds, matching cold_open's convention) or None
    if generation/parsing/validation fails for any reason — never raises, so
    a bad beat plan just means the caller falls back to no beat plan, the
    same tolerance cold_open gets in llm_scoring.score_batch."""
    segs = _clip_segments(segments, clip["start"], clip["end"])
    if not segs:
        return None

    numbered = "\n".join(f"[{i}] {s['start']:.1f}s-{s['end']:.1f}s: {s['text'].strip()}" for i, s in enumerate(segs))

    try:
        client = Anthropic()
        message = client.messages.create(
            model=MODEL,
            max_tokens=1536,
            output_config={"effort": "low"},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Clip transcript:\n{numbered}"}],
        )
    except Exception as e:
        print(f"  [warn] beat plan generation failed for clip [{clip['start']:.1f}-{clip['end']:.1f}]: {e}")
        return None

    text_block = next((b for b in message.content if b.type == "text"), None)
    if text_block is None:
        return None
    try:
        raw_items = json.loads(_strip_json_fence(text_block.text))
    except json.JSONDecodeError as e:
        print(f"  [warn] beat plan response wasn't valid JSON for clip [{clip['start']:.1f}-{clip['end']:.1f}]: {e}")
        return None
    if not isinstance(raw_items, list) or not raw_items:
        return None

    beats = []
    prev_end_idx = -1
    for raw in raw_items:
        try:
            beat_type = raw["beat_type"]
            if beat_type not in BEAT_TYPES:
                raise ValueError(f"unknown beat_type {beat_type!r}")
            start_idx = int(raw["start_segment"])
            end_idx = int(raw["end_segment"])
            if not (0 <= start_idx <= end_idx < len(segs)):
                raise ValueError(f"segment indices {start_idx}-{end_idx} out of range for {len(segs)} segments")
            if start_idx <= prev_end_idx:
                raise ValueError(f"beat {beat_type!r} overlaps the previous beat (segment {start_idx} <= {prev_end_idx})")

            start = segs[start_idx]["start"]
            end = segs[end_idx]["end"]
            bounds = BEAT_DURATION_BOUNDS.get(beat_type)
            if bounds and not (bounds[0] <= end - start <= bounds[1]):
                raise ValueError(f"{beat_type!r} duration {end - start:.1f}s outside {bounds[0]}-{bounds[1]}s")

            beats.append({
                "beat_type": beat_type,
                "start": start,
                "end": end,
                "note": str(raw.get("note", "")).strip(),
                "emphasis_word": (str(raw["emphasis_word"]).strip() if raw.get("emphasis_word") else None),
            })
            prev_end_idx = end_idx
        except (KeyError, ValueError, TypeError) as e:
            print(f"  [warn] dropping invalid beat for clip [{clip['start']:.1f}-{clip['end']:.1f}]: {e}")
            continue

    return beats or None


if __name__ == "__main__":
    import sys

    sys.path.insert(0, os.path.dirname(__file__))
    from pipeline import transcribe
    from scoring import score_video

    path = sys.argv[1] if len(sys.argv) > 1 else "downloads/lgaR7Z9okYM.mp4"
    print(f"Transcribing {path} ...")
    segments = transcribe(path, model_size="small")

    print("Scoring (LLM + audio energy) ...")
    clips, _ = score_video(segments, path, top_n=5)

    clip = clips[0]
    print(f"\nGenerating beat plan for clip [{clip['start']:.1f}-{clip['end']:.1f}] ...")
    beats = generate_beat_plan(clip, segments)
    if not beats:
        print("No beat plan generated.")
    else:
        for b in beats:
            extra = f" note={b['note']!r}" if b["note"] else f" emphasis={b['emphasis_word']!r}" if b["emphasis_word"] else ""
            print(f"  [{b['start']:6.1f}-{b['end']:6.1f}] {b['beat_type']:15s}{extra}")
