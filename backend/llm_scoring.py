"""
LLM scoring pass (PRD section 4A + 6.2): chunk the transcript into sliding
candidate windows, ask Claude to score each 0-10 as a standalone short clip
and flag emphasis words for captioning, batching several windows into each
API call.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import numpy as np
from dotenv import load_dotenv
from anthropic import Anthropic

from audio_energy import window_energy_score

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

MODEL = os.environ.get("CLAUDE_SCORING_MODEL", "claude-sonnet-5")
SCORING_CONCURRENCY = int(os.environ.get("SCORING_CONCURRENCY", "8"))
SCORING_BATCH_SIZE = int(os.environ.get("SCORING_BATCH_SIZE", "4"))

SYSTEM_PROMPT = """You evaluate excerpts from a longer video transcript for their \
potential as standalone short-form clips (YouTube Shorts / Reels / TikTok), and \
choose the best clip boundary within each excerpt rather than using the whole thing.

You will be given several independent candidate WINDOWS from the same video, each \
labeled "Window N" with its own NUMBERED list of transcript segments (with time \
ranges). Evaluate every window separately — segment numbering resets per window, \
so segment [0] in Window 2 is not the same as segment [0] in Window 0. For each \
window, choose a contiguous range of segments (start_segment to end_segment, \
inclusive) that forms one complete, standalone narrative or thought — with a clear \
hook, a clean ending, and nothing important cut off mid-sentence. Prefer a total \
duration between 60 and 90 seconds when the segments allow it, but don't pad with \
irrelevant material just to reach 90, and don't cut a thought short just to reach \
60 — a tighter or longer clip is fine if that's genuinely what the content needs.

Respond with a single JSON array (no prose, no markdown fences), one object per \
window, in the same order the windows were given. Each object must have these \
fields:
- "window_index": integer, the N from that window's "Window N" label
- "start_segment": integer, index of the first segment to include (within that \
window's own numbering)
- "end_segment": integer, index of the last segment to include, inclusive
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


def build_windows(
    segments: list[dict],
    min_len: float = 20.0,
    max_len: float = 90.0,
    default_stride: float = 45.0,
    target_windows: int = 120,
) -> list[dict]:
    """Turn whisper segments into overlapping sliding-window search zones. Each
    window carries the raw segments (natural sentence/phrase units) so Claude
    can pick a sub-range within it as the actual clip, rather than the window
    itself always being the clip — see score_batch.

    Stride is `default_stride` for anything short enough to stay under
    `target_windows` at that stride; for longer videos it widens (capped at
    `max_len`, so windows still tile the video with no coverage gaps) to hold
    the total window count near `target_windows` instead of growing without
    bound.
    """
    if not segments:
        return []

    total_end = segments[-1]["end"]
    stride = max(default_stride, total_end / target_windows)
    stride = min(stride, max_len)

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


def filter_low_energy(windows: list[dict], energy: dict, percentile: float = 10.0) -> list[dict]:
    """Drop the quietest/least-eventful windows (likely dead air or
    music-only stretches) before spending an LLM call on them. Threshold is
    a percentile of this video's own window-energy distribution, not an
    absolute cutoff, so it adapts to each video's dynamic range."""
    if not windows:
        return windows
    scores = [window_energy_score(energy, w["start"], w["end"]) for w in windows]
    threshold = float(np.percentile(scores, percentile))
    return [w for w, s in zip(windows, scores) if s >= threshold]


def _chunk(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def score_batch(client: Anthropic, batch: list[dict]) -> list[dict]:
    parts = []
    for i, window in enumerate(batch):
        segs = window["segments"]
        numbered = "\n".join(f"[{j}] {s['start']:.1f}s-{s['end']:.1f}s: {s['text'].strip()}" for j, s in enumerate(segs))
        parts.append(f"Window {i}:\n{numbered}")
    prompt = "\n\n".join(parts)

    message = client.messages.create(
        model=MODEL,
        max_tokens=1024 * len(batch),
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": f"Candidate windows:\n\n{prompt}"}],
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
    results_raw = json.loads(raw)
    if not isinstance(results_raw, list):
        raise ValueError("expected a JSON array of per-window results")

    by_index = {}
    for item in results_raw:
        idx = item.get("window_index")
        if idx is not None:
            by_index[int(idx)] = item

    out = []
    for i, window in enumerate(batch):
        result = by_index.get(i)
        if result is None:
            print(f"  [warn] no result for window [{window['start']:.1f}-{window['end']:.1f}] in batch response")
            continue
        segs = window["segments"]
        try:
            start_idx = int(result["start_segment"])
            end_idx = int(result["end_segment"])
            if not (0 <= start_idx <= end_idx < len(segs)):
                raise ValueError(f"segment indices {start_idx}-{end_idx} out of range for {len(segs)} segments")
        except (KeyError, ValueError) as e:
            print(f"  [warn] bad result for window [{window['start']:.1f}-{window['end']:.1f}]: {e}")
            continue
        chosen = segs[start_idx : end_idx + 1]

        out.append({
            "start": chosen[0]["start"],
            "end": chosen[-1]["end"],
            "text": " ".join(s["text"].strip() for s in chosen),
            "llm_score": float(result["score"]) / 10.0,
            "reason": result["reason"],
            "emphasis_words": result.get("emphasis_words", []),
            "hook": result.get("hook", ""),
            "seo_title": result.get("seo_title", ""),
            "seo_description": result.get("seo_description", ""),
        })
    return out


def score_all_windows(
    segments: list[dict],
    energy: Optional[dict] = None,
    energy_percentile: float = 10.0,
    **window_kwargs,
) -> list[dict]:
    client = Anthropic()
    windows = build_windows(segments, **window_kwargs)
    if energy is not None:
        windows = filter_low_energy(windows, energy, energy_percentile)

    batches = list(_chunk(windows, SCORING_BATCH_SIZE))

    scored = []
    with ThreadPoolExecutor(max_workers=SCORING_CONCURRENCY) as executor:
        future_to_batch = {executor.submit(score_batch, client, b): b for b in batches}
        for future in as_completed(future_to_batch):
            b = future_to_batch[future]
            try:
                scored.extend(future.result())
            except Exception as e:
                print(f"  [warn] scoring failed for batch starting [{b[0]['start']:.1f}s]: {e}")
    return scored


if __name__ == "__main__":
    import sys

    sys.path.insert(0, os.path.dirname(__file__))
    from pipeline import transcribe
    from audio_energy import analyze_energy

    path = sys.argv[1] if len(sys.argv) > 1 else "downloads/lgaR7Z9okYM.mp4"
    print(f"Transcribing {path} ...")
    segments = transcribe(path, model_size="small")

    print(f"Scoring {len(segments)} segments' worth of sliding windows via Claude ...")
    energy = analyze_energy(path)
    scored = score_all_windows(segments, energy=energy)

    scored.sort(key=lambda c: c["llm_score"], reverse=True)
    for c in scored:
        print(f"[{c['start']:6.1f}-{c['end']:6.1f}] score={c['llm_score']:.2f}  {c['reason']}")
        if c["emphasis_words"]:
            print(f"           emphasis: {c['emphasis_words']}")
