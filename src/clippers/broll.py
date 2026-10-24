"""
B-roll engine (PRD sections 7-8): for each clip, turn its script into concrete
visual search queries (never the raw sentence — see QUERY_SYSTEM_PROMPT), then
search Pexels for stock footage and take its top-ranked result per query.

Requires PEXELS_API_KEY in your .env. Nothing here downloads footage into a
render automatically — it returns picks (source link + query) for a human to
review, matching the rest of this project's "human always approves" posture
(PRD section 13). Called on-demand per clip, not automatically during
/analyze — each call still runs one Claude call for query generation, so
it isn't free to run unprompted for every candidate clip.
"""

import json
import os
import re
from typing import Optional

import requests
from anthropic import Anthropic

PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")
QUERY_MODEL = os.environ.get("CLAUDE_BROLL_QUERY_MODEL", "claude-sonnet-5")

# One Pexels top-result pick per query, so this is also the standard total
# number of B-roll videos returned per clip.
DEFAULT_QUERIES_PER_CLIP = int(os.environ.get("BROLL_QUERIES_PER_CLIP", "6"))

PEXELS_VIDEO_SEARCH_URL = "https://api.pexels.com/videos/search"

QUERY_SYSTEM_PROMPT = """You convert a short-form video clip's spoken content into \
concrete visual search queries for stock footage (Pexels) — B-roll cutaways \
that play under the narration.

Never search the sentence itself. A search term must name a literal, filmable subject: \
a person doing something, an object, a place, a weather condition, an action. Abstract \
or emotional words in the clip ("motivation," "discipline," "consistency") must be \
translated into a concrete visual that evokes them, not searched as-is.

For example, for the line "You keep waiting for motivation to strike before you start," \
good queries are "person staring at phone", "alarm clock morning", "empty running shoes \
by door" — not "motivation" or "waiting for motivation."

Given a clip's transcript (and optionally its distilled core idea), return the requested \
number of short visual search queries (2-5 words each), each naming a different concrete \
subject so the results don't all look the same. For each query, also give the "quote" \
that inspired it: a short excerpt (4-10 consecutive words) copied VERBATIM from the \
transcript, exactly as written there — this is used to time where the B-roll cutaway \
plays, so it must be an exact substring match, not a paraphrase. Respond with a single \
JSON array of objects, no prose, no markdown fences: [{"query": "...", "quote": "..."}, ...]"""


def _strip_json_fence(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw


def generate_broll_queries(clip_text: str, core_idea: str = "", n: int = DEFAULT_QUERIES_PER_CLIP) -> list[dict]:
    """PRD §8: turn a clip's script into concrete visual search queries
    instead of searching the sentence verbatim. Returns a list of
    {"query": str, "quote": str} — `quote` is the transcript excerpt that
    inspired the query, used by locate_quote_offset to time the cutaway."""
    context = clip_text if not core_idea else f"{clip_text}\n\nCore idea: {core_idea}"
    client = Anthropic()
    message = client.messages.create(
        model=QUERY_MODEL,
        max_tokens=256,
        output_config={"effort": "low"},
        system=QUERY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Clip transcript:\n{context}\n\nReturn {n} visual search queries."}],
    )
    text_block = next((b for b in message.content if b.type == "text"), None)
    if text_block is None:
        return []
    try:
        raw_items = json.loads(_strip_json_fence(text_block.text))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw_items, list):
        return []

    items = []
    for it in raw_items[:n]:
        if not isinstance(it, dict):
            continue
        query = str(it.get("query", "")).strip()
        quote = str(it.get("quote", "")).strip()
        if query:
            items.append({"query": query, "quote": quote})
    return items


def _search_pexels(query: str, per_page: int) -> list[dict]:
    resp = requests.get(
        PEXELS_VIDEO_SEARCH_URL,
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": query, "per_page": per_page, "orientation": "portrait"},
        timeout=15,
    )
    resp.raise_for_status()
    candidates = []
    for v in resp.json().get("videos", []):
        # Pick the highest-resolution file Pexels offers for this video as
        # the preview/download link — good enough for a human reviewing the
        # suggestion; actual delivery-quality re-encoding happens in Resolve.
        files = sorted(
            v.get("video_files", []),
            key=lambda f: (f.get("width") or 0) * (f.get("height") or 0),
            reverse=True,
        )
        file = files[0] if files else None
        candidates.append({
            "provider": "pexels",
            "id": v.get("id"),
            "page_url": v.get("url", ""),
            "preview_url": file["link"] if file else "",
            "thumbnail_url": v.get("image", ""),
            "width": v.get("width"),
            "height": v.get("height"),
            "duration": v.get("duration"),
        })
    return candidates


def search_broll(query: str) -> Optional[dict]:
    """Search Pexels and return its top-ranked result for `query` (Pexels
    already ranks by relevance, so no separate selection step is needed),
    or None if Pexels is unconfigured, errors, or returns nothing."""
    if not PEXELS_API_KEY:
        return None
    try:
        candidates = _search_pexels(query, per_page=1)
    except requests.RequestException:
        return None
    return candidates[0] if candidates else None


def _normalize_word(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _clip_words(segments: list[dict], clip_start: float, clip_end: float) -> list[dict]:
    words = []
    for seg in segments:
        if seg.get("end", 0) < clip_start or seg.get("start", 0) > clip_end:
            continue
        for w in seg.get("words", []):
            if w["start"] >= clip_start - 0.05 and w["end"] <= clip_end + 0.05:
                words.append(w)
    return words


def locate_quote_offset(quote: str, segments: list[dict], clip_start: float, clip_end: float) -> Optional[float]:
    """Best-effort: find where `quote` (the transcript excerpt Claude says
    inspired a B-roll query) occurs in the clip's word-level transcript, so
    the cutaway can be placed at that point instead of always at the start.
    Anchors on the first quote word that appears as a whole word in the
    clip — an exact substring match on the raw transcript is too brittle
    (Whisper casing/punctuation, minor rewording by Claude) to require a
    full-phrase match, and the neighborhood is all timing needs to be
    roughly right. Returns None (caller falls back to clip start) if no
    quote word appears in this clip's transcript at all."""
    words = _clip_words(segments, clip_start, clip_end)
    if not words or not quote:
        return None
    quote_tokens = [t for t in (_normalize_word(t) for t in quote.split()) if len(t) > 2]
    for token in quote_tokens:
        for w in words:
            if _normalize_word(w["word"]) == token:
                return round(w["start"] - clip_start, 2)
    return None


def download_broll_file(url: str, dest_path: str) -> str:
    """Download a picked B-roll's preview_url to a local file — Resolve's
    scripting API only accepts local filesystem paths, not remote URLs."""
    resp = requests.get(url, stream=True, timeout=30)
    resp.raise_for_status()
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            f.write(chunk)
    return dest_path


def find_broll_for_beats(clip: dict, beat_plan: list[dict]) -> list[dict]:
    """B-roll placement driven by an explicit beat plan (beat_plan.py)
    instead of freeform query generation: one pick per "broll"/"broll_sound"
    beat, searched directly on that beat's own `note` (already written to
    the same concreteness bar as a Pexels query — see beat_plan.py's
    SYSTEM_PROMPT), with `offset`/`duration` taken exactly from the beat's
    own start/end rather than locate_quote_offset's fuzzy first-word match.
    Zero extra Claude calls beyond the beat plan itself, which the caller
    already generated separately."""
    if not PEXELS_API_KEY:
        raise RuntimeError("PEXELS_API_KEY is not set — add it to your .env (~/Clippers/.env) to fetch B-roll.")

    picks = []
    for beat in beat_plan:
        if beat.get("beat_type") not in ("broll", "broll_sound"):
            continue
        note = (beat.get("note") or "").strip()
        if not note:
            continue
        pick = search_broll(note)
        if not pick:
            continue
        pick["query"] = note
        pick["quote"] = ""
        pick["offset"] = round(beat["start"] - clip["start"], 2)
        pick["duration"] = round(beat["end"] - beat["start"], 2)
        picks.append(pick)
    return picks


def find_broll_for_clip(
    clip: dict,
    queries_per_clip: int = DEFAULT_QUERIES_PER_CLIP,
    segments: Optional[list[dict]] = None,
    beat_plan: Optional[list[dict]] = None,
) -> list[dict]:
    """Full B-roll pipeline for one already-selected clip: generate visual
    search queries from its text/core idea (§8), then search Pexels and take
    its top-ranked result per query (§7) — Pexels' own relevance ranking is
    the selection, no separate AI vision pass. Returns one pick per query
    that found a result: up to `queries_per_clip` videos total, which is
    also this project's standard B-roll count per clip. Purely a suggestion
    list for a human to review/select — nothing downloads or composites
    until a human picks one and sends it to Resolve (see main.py's
    /send-to-resolve).

    When `beat_plan` is given (and non-empty), placement is delegated to
    find_broll_for_beats instead — precise, beat-anchored offsets rather
    than this function's own freeform query generation + fuzzy quote match.

    When `segments` (the video's word-level transcript) is given, each pick
    also gets an "offset" (seconds from the clip's own start) computed from
    its query's source quote via locate_quote_offset, so a selected pick
    can be placed at the right point in the clip rather than always at the
    very start."""
    if beat_plan:
        return find_broll_for_beats(clip, beat_plan)

    if not PEXELS_API_KEY:
        raise RuntimeError("PEXELS_API_KEY is not set — add it to your .env (~/Clippers/.env) to fetch B-roll.")

    clip_text = clip.get("text", "")
    core_idea = clip.get("core_idea", "")
    query_items = generate_broll_queries(clip_text, core_idea, n=queries_per_clip)

    picks = []
    for item in query_items:
        query = item["query"]
        quote = item.get("quote", "")
        pick = search_broll(query)
        if not pick:
            continue
        pick["query"] = query
        pick["quote"] = quote
        if segments is not None:
            pick["offset"] = locate_quote_offset(quote, segments, clip["start"], clip["end"])
        picks.append(pick)
    return picks


if __name__ == "__main__":
    import sys

    from clippers.pipeline import transcribe
    from clippers.scoring import score_video

    path = sys.argv[1] if len(sys.argv) > 1 else "downloads/lgaR7Z9okYM.mp4"
    print(f"Transcribing {path} ...")
    segments = transcribe(path, model_size="small")

    print("Scoring (LLM + audio energy) ...")
    clips = score_video(segments, path, top_n=5)

    clip = clips[0]
    print(f"\nFinding B-roll for clip [{clip['start']:.1f}-{clip['end']:.1f}] ...")
    picks = find_broll_for_clip(clip)
    for p in picks:
        print(f"\nQuery: {p['query']!r}  (provider={p['provider']})")
        print(f"  {p['page_url']}")
