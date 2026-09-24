"""
Content transformation stage (PRD section 6): for each selected clip,
generate the full structured editorial package — a distilled core idea,
original written commentary, an actionable takeaway, a CTA, and a pair of
short spoken bookends (intro/close) for narration. Hook already comes out
of llm_scoring.py; this covers the other four PRD §6 fields plus the
spoken bookend scripts a future TTS/render stage would use.

The written "commentary" field is also the substance that makes a reused-
content video transformative under YouTube's reused-content policy, per
the "would this still work if my added part were deleted?" test.

Runs once per video on the already-selected top_n clips (not on every
candidate window scoring considered), so it costs one small batched call
per video rather than one per window.
"""

import json
import os
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

MODEL = os.environ.get("CLAUDE_COMMENTARY_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = """You produce the structured editorial package that turns a raw \
sermon/talk clip into a short-form post on a curated Christian commentary channel: a \
distilled core idea, original written commentary, an actionable takeaway, a \
call-to-action, and a pair of short spoken bookends for narration.

Frame every field around the IDEA the clip illustrates (discipline, patience, money, \
relationships, and so on) — not around the speaker's identity. The clip is evidence \
supporting the lesson, not the star of the video; never write as if the point is \
"listen to what this person said."

The channel is working toward YouTube monetization under the "reused content" policy, \
which requires the added material to be genuinely transformative — YouTube's own test is \
"would this video still work if my added part were deleted?" A caption, hook, or \
watermark fails that test because it's packaging, not transformation. Your "commentary" \
field must pass it: real analysis, context, or a related thought that stands on its own \
and changes what the clip means to the viewer — never a recap, a tease, or a "watch \
this" style intro.

You will be given several clips from the same video, each with its transcript text, \
why it was selected, and the source video's title. For EACH clip, write:
- "core_idea": one sentence — the lesson or thesis the clip illustrates, in your own \
words. Not a quote, not the clickbait hook — the plain, sober version of the point.
- "commentary": 2-4 sentences of original written analysis or context that extends the \
clip's point — substantive material a viewer couldn't get from the clip alone.
- "takeaway": one short, actionable sentence — what a viewer should actually do or \
remember. Distinct from core_idea, not a restatement of it.
- "cta": one short call-to-action sentence tied to the idea (e.g. "Follow for more \
lessons on staying consistent when motivation runs out.") — not a generic "like and \
subscribe."
- a short spoken INTRO (roughly 8-16 words / 3-5 seconds aloud) — sets up or reframes \
the clip with your own angle, not a summary of what's about to be said
- a short spoken CLOSE (roughly 8-16 words / 3-5 seconds aloud) — extends the thought \
after the clip ends

Vary your angle across the clips you're given; do not reuse the same structure, opening \
phrase, or format twice in this batch. Draw from different approaches as appropriate to \
each clip's content, for example:
- a related historical or theological detail the clip doesn't mention
- a pointed question the clip's content raises
- a brief personal or contemporary application
- a respectful counterpoint or complication
- connecting the clip to a broader pattern or theme

Respond with a single JSON array (no prose, no markdown fences), one object per clip, \
in the same order given. Each object must have:
- "clip_index": integer, the index of the clip as given
- "angle": short label for the approach you used (e.g. "historical_context", \
"question_to_viewer", "personal_application", "counterpoint", "broader_theme")
- "core_idea": string
- "commentary": string
- "takeaway": string
- "cta": string
- "commentary_intro": string
- "commentary_close": string
"""


def generate_commentary_for_clips(
    clips: list[dict],
    video_title: str,
    source: Optional[str] = None,
) -> list[dict]:
    """Generate the full PRD §6 content-transformation package for each
    already-selected clip in one batched call. Returns a list the same
    length/order as `clips`, each item {"core_idea", "commentary",
    "takeaway", "cta", "commentary_intro", "commentary_close",
    "commentary_angle"} — empty strings for any clip the model didn't return
    a usable result for, so callers can merge positionally without
    special-casing failures."""
    if not clips:
        return []

    client = Anthropic()

    parts = [
        f"Clip {i} (why selected: {clip.get('reason', '')}):\n{clip['text']}"
        for i, clip in enumerate(clips)
    ]
    source_line = f"Source: {source}\n" if source else ""
    prompt = f"Video: {video_title}\n{source_line}\n" + "\n\n".join(parts)

    message = client.messages.create(
        model=MODEL,
        # claude-sonnet-5 thinks by default (adaptive); a too-small max_tokens
        # can be exhausted entirely by thinking before any text is emitted,
        # leaving message.content with a thinking block and no text block at
        # all. This is a short writing task, so effort=low keeps thinking
        # brief rather than disabling it outright, and the token floor leaves
        # headroom regardless. Per-clip budget is higher than the old
        # intro/close-only prompt since each clip now returns seven fields
        # instead of two.
        max_tokens=max(4096, 1536 * len(clips)),
        output_config={"effort": "low"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    text_block = next((b for b in message.content if b.type == "text"), None)
    if text_block is None:
        raise RuntimeError(f"No text block in Claude response (got: {[b.type for b in message.content]})")
    raw = text_block.text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    results_raw = json.loads(raw)
    if not isinstance(results_raw, list):
        raise ValueError("expected a JSON array of per-clip results")

    by_index = {}
    for item in results_raw:
        idx = item.get("clip_index")
        if idx is not None:
            by_index[int(idx)] = item

    empty = {
        "core_idea": "", "commentary": "", "takeaway": "", "cta": "",
        "commentary_intro": "", "commentary_close": "", "commentary_angle": "",
    }
    out = []
    for i in range(len(clips)):
        result = by_index.get(i)
        if result is None:
            out.append(dict(empty))
            continue
        out.append({
            "core_idea": result.get("core_idea", ""),
            "commentary": result.get("commentary", ""),
            "takeaway": result.get("takeaway", ""),
            "cta": result.get("cta", ""),
            "commentary_intro": result.get("commentary_intro", ""),
            "commentary_close": result.get("commentary_close", ""),
            "commentary_angle": result.get("angle", ""),
        })
    return out


if __name__ == "__main__":
    import sys

    sys.path.insert(0, os.path.dirname(__file__))
    from pipeline import transcribe
    from scoring import score_video

    path = sys.argv[1] if len(sys.argv) > 1 else "downloads/lgaR7Z9okYM.mp4"
    print(f"Transcribing {path} ...")
    segments = transcribe(path, model_size="small")

    print("Scoring (LLM + audio energy) ...")
    clips = score_video(segments, path, top_n=5)

    print(f"Generating commentary for {len(clips)} clips ...")
    commentary = generate_commentary_for_clips(clips, video_title=os.path.basename(path))

    for c, com in zip(clips, commentary):
        print(f"\n[{c['start']:.1f}-{c['end']:.1f}] angle={com['commentary_angle']}")
        print(f"  CORE IDEA: {com['core_idea']}")
        print(f"  COMMENTARY: {com['commentary']}")
        print(f"  TAKEAWAY: {com['takeaway']}")
        print(f"  CTA: {com['cta']}")
        print(f"  INTRO: {com['commentary_intro']}")
        print(f"  CLOSE: {com['commentary_close']}")
