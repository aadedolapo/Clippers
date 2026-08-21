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

SYSTEM_PROMPT = """You evaluate short excerpts from a longer video transcript for their \
potential as standalone short-form clips (YouTube Shorts / Reels / TikTok).

For the given transcript excerpt, respond with a single JSON object (no prose, no \
markdown fences) with these fields:
- "score": integer 0-10, how well this excerpt works as a standalone short clip
- "reason": one short sentence explaining the score (e.g. "strong hook line", \
"punchline lands", "emotional beat", "no clear hook, rambling")
- "emphasis_words": array of strings, the exact words from the excerpt (numbers, \
punchlines, surprising claims) that should be visually emphasized in captions. \
Empty array if none stand out.
- "hook": a punchy 3-6 word on-screen hook line (not a direct quote from the \
excerpt) that would make someone stop scrolling in the first second — the kind \
of text overlay shown at the very start of a Short before the speaker's own \
words carry it. E.g. "She said WHAT to her boss?", "The $240 mistake", "Nobody \
tells you this about excellence".

Score high (7-10) for: a strong opening hook, a punchline or payoff, a surprising \
claim or number, a complete emotional or narrative beat.
Score low (0-3) for: rambling with no clear point, mid-thought fragments, filler.
"""


def build_windows(segments: list[dict], min_len: float = 20.0, max_len: float = 90.0, stride: float = 20.0) -> list[dict]:
    """Turn whisper segments into overlapping sliding-window candidate clips."""
    if not segments:
        return []

    total_end = segments[-1]["end"]
    windows = []
    start = 0.0
    while start < total_end:
        end = min(start + max_len, total_end)
        if end - start >= min_len or end == total_end:
            words_in_window = [
                w for seg in segments for w in seg["words"] if w["start"] >= start and w["end"] <= end
            ]
            text = " ".join(w["word"] for w in words_in_window).strip()
            if text:
                windows.append({"start": start, "end": end, "text": text, "words": words_in_window})
        start += stride
    return windows


def score_window(client: Anthropic, window: dict) -> dict:
    message = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Transcript excerpt:\n\n{window['text']}"}],
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
    return {
        "start": window["start"],
        "end": window["end"],
        "text": window["text"],
        "llm_score": float(result["score"]) / 10.0,
        "reason": result["reason"],
        "emphasis_words": result.get("emphasis_words", []),
        "hook": result.get("hook", ""),
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
