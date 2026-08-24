"""
LLM scoring pass (PRD section 4A + 6.2): chunk the transcript into sliding
candidate windows, ask Claude to score each 0-10 as a standalone short clip
and flag emphasis words for captioning, in a single call per window.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

MODEL = os.environ.get("CLAUDE_SCORING_MODEL", "claude-sonnet-5")
SCORING_CONCURRENCY = int(os.environ.get("SCORING_CONCURRENCY", "8"))

SYSTEM_PROMPT = """You evaluate excerpts from a longer video transcript for their \
potential as standalone short-form clips (YouTube Shorts / Reels / TikTok), and \
choose the best clip boundary within the excerpt rather than using the whole thing.

You will be given a NUMBERED list of transcript segments, each with its time range. \
Choose a contiguous range of segments (start_segment to end_segment, inclusive) that \
forms one complete, standalone narrative or thought — with a clear hook, a clean \
ending, and nothing important cut off mid-sentence. Prefer a total duration between \
60 and 90 seconds when the segments allow it, but don't pad with irrelevant material \
just to reach 90, and don't cut a thought short just to reach 60 — a tighter or \
longer clip is fine if that's genuinely what the content needs.

Respond with a single JSON object (no prose, no markdown fences) with these fields:
- "start_segment": integer, index of the first segment to include
- "end_segment": integer, index of the last segment to include (inclusive)
- "score": integer 0-10, how well the resulting clip works as a standalone short clip
- "reason": one short sentence explaining the score (e.g. "strong hook line", \
"punchline lands", "emotional beat", "no clear hook, rambling")
- "emphasis_words": array of strings, the exact words from the chosen clip (numbers, \
punchlines, surprising claims) that should be visually emphasized in captions. \
Empty array if none stand out.
- "hook": a CLICKBAIT-style on-screen hook line, 3-7 words, meant to stop a scroll \
in under a second. Not a summary or direct quote of the clip — it must create a \
curiosity gap that only watching resolves, without giving away the payoff. Favor \
these patterns:
  * a provocative question ("Why does nobody talk about this?")
  * a specific, surprising number ("The $240 mistake I made monthly")
  * a bold claim that begs "how?" ("One sentence changed everything")
  * direct address that implicates the viewer ("You're doing this wrong")
  * a withheld payoff ("She said WHAT to her boss?")
Avoid flat descriptions ("A story about overcoming fear"), generic phrasing \
("Amazing life lesson"), and anything that spoils the ending.
- "seo_title": a searchable, keyword-rich title for the platform's title/caption \
field — NOT the same job as "hook". Where the hook is pure curiosity-gap bait \
for the on-screen overlay, this is what actually gets typed into YouTube \
Shorts/TikTok/Instagram when posting, so it needs to surface in search and \
recommendations: lead with the concrete topic/subject and the main keyword \
someone would actually search for, stay accurate to the clip's content, and \
keep it under about 70 characters. Can still be compelling, just not at the \
cost of being findable.
- "seo_description": a 1-2 sentence caption for the post that naturally works in \
relevant keywords and context (who/what/why someone would care), followed by \
3-5 relevant hashtags (e.g. "#mindset #discipline #selfimprovement").

Score high (7-10) for: a strong opening hook, a punchline or payoff, a surprising \
claim or number, a complete emotional or narrative beat.
Score low (0-3) for: rambling with no clear point, mid-thought fragments, filler.
"""


def build_windows(segments: list[dict], min_len: float = 20.0, max_len: float = 90.0, stride: float = 20.0) -> list[dict]:
    """Turn whisper segments into overlapping sliding-window search zones. Each
    window carries the raw segments (natural sentence/phrase units) so Claude
    can pick a sub-range within it as the actual clip, rather than the window
    itself always being the clip — see score_window."""
    if not segments:
        return []

    total_end = segments[-1]["end"]
    windows = []
    start = 0.0
    while start < total_end:
        end = min(start + max_len, total_end)
        if end - start >= min_len or end == total_end:
            segs_in_window = [s for s in segments if s["start"] < end and s["end"] > start]
            if segs_in_window:
                windows.append({"start": start, "end": end, "segments": segs_in_window})
        start += stride
    return windows


def score_window(client: Anthropic, window: dict) -> dict:
    segs = window["segments"]
    numbered = "\n".join(f"[{i}] {s['start']:.1f}s-{s['end']:.1f}s: {s['text'].strip()}" for i, s in enumerate(segs))

    message = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Transcript segments:\n\n{numbered}"}],
    )
    # content[0] isn't reliably the text block — the model can emit a
    # ThinkingBlock (or other non-text block) first, which has no .text
    # attribute. Find the actual text block regardless of position.
    text_block = next((block for block in message.content if block.type == "text"), None)
    if text_block is None:
        raise RuntimeError(f"No text block in Claude response (got: {[b.type for b in message.content]})")
    raw = text_block.text.strip()
    # Models sometimes wrap JSON in fences despite instructions; strip if present.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    result = json.loads(raw)

    start_idx = int(result["start_segment"])
    end_idx = int(result["end_segment"])
    if not (0 <= start_idx <= end_idx < len(segs)):
        raise ValueError(f"segment indices {start_idx}-{end_idx} out of range for {len(segs)} segments")
    chosen = segs[start_idx : end_idx + 1]

    return {
        "start": chosen[0]["start"],
        "end": chosen[-1]["end"],
        "text": " ".join(s["text"].strip() for s in chosen),
        "llm_score": float(result["score"]) / 10.0,
        "reason": result["reason"],
        "emphasis_words": result.get("emphasis_words", []),
        "hook": result.get("hook", ""),
        "seo_title": result.get("seo_title", ""),
        "seo_description": result.get("seo_description", ""),
    }


def score_all_windows(segments: list[dict], **window_kwargs) -> list[dict]:
    client = Anthropic()
    windows = build_windows(segments, **window_kwargs)
    scored = []
    with ThreadPoolExecutor(max_workers=SCORING_CONCURRENCY) as executor:
        future_to_window = {executor.submit(score_window, client, w): w for w in windows}
        for future in as_completed(future_to_window):
            w = future_to_window[future]
            try:
                scored.append(future.result())
            except Exception as e:
                print(f"  [warn] scoring failed for window [{w['start']:.1f}-{w['end']:.1f}]: {e}")
    return scored


if __name__ == "__main__":
    import sys

    sys.path.insert(0, os.path.dirname(__file__))
    from pipeline import transcribe

    path = sys.argv[1] if len(sys.argv) > 1 else "downloads/lgaR7Z9okYM.mp4"
    print(f"Transcribing {path} ...")
    segments = transcribe(path, model_size="small")

    print(f"Scoring {len(segments)} segments' worth of sliding windows via Claude ...")
    scored = score_all_windows(segments)

    scored.sort(key=lambda c: c["llm_score"], reverse=True)
    for c in scored:
        print(f"[{c['start']:6.1f}-{c['end']:6.1f}] score={c['llm_score']:.2f}  {c['reason']}")
        if c["emphasis_words"]:
            print(f"           emphasis: {c['emphasis_words']}")
