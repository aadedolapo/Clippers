"""
LLM scoring pass: chunk the transcript into sliding
candidate windows, ask Claude to score each 0-10 as a standalone short clip
and flag emphasis words for captioning, batching several windows into each
API call.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

import numpy as np
from anthropic import Anthropic

from clippers import channel, diarize, performance
from clippers.audio_energy import window_energy_score

MODEL = os.environ.get("CLAUDE_SCORING_MODEL", "claude-sonnet-5")
SCORING_CONCURRENCY = int(os.environ.get("SCORING_CONCURRENCY", "8"))
SCORING_BATCH_SIZE = int(os.environ.get("SCORING_BATCH_SIZE", "4"))


@dataclass(frozen=True)
class ClipLength:
    """Allowed clip duration (hard bounds) and the range the prompt asks
    the model to aim for."""
    min_s: float
    max_s: float
    target_min_s: float
    target_max_s: float

    def window_len(self) -> float:
        # Search windows need room for a whole clip plus some context.
        return max(60.0, self.max_s + 15.0)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# Default Shorts length, overridable in your .env.
SHORT = ClipLength(
    min_s=_env_float("CLIP_MIN_SECONDS", 15.0),
    max_s=_env_float("CLIP_MAX_SECONDS", 45.0),
    target_min_s=_env_float("CLIP_TARGET_MIN_SECONDS", 20.0),
    target_max_s=_env_float("CLIP_TARGET_MAX_SECONDS", 35.0),
)
# Long-form story clips, picked per job with AnalyzeRequest.long_form.
LONG = ClipLength(
    min_s=_env_float("LONG_CLIP_MIN_SECONDS", 45.0),
    max_s=_env_float("LONG_CLIP_MAX_SECONDS", 90.0),
    target_min_s=_env_float("LONG_CLIP_TARGET_MIN_SECONDS", 60.0),
    target_max_s=_env_float("LONG_CLIP_TARGET_MAX_SECONDS", 75.0),
)

SYSTEM_PROMPT = """You evaluate excerpts from a longer video transcript for their \
potential as standalone short-form clips (YouTube Shorts / Reels / TikTok), and \
choose the best clip boundary within each excerpt rather than using the whole thing. \
The clips are for {channel}.

You will be given several independent candidate WINDOWS from the same video, each \
labeled "Window N" with its own NUMBERED list of transcript segments (with time \
ranges). Evaluate every window separately — segment numbering resets per window, \
so segment [0] in Window 2 is not the same as segment [0] in Window 0. For each \
window, choose a contiguous range of segments (start_segment to end_segment, \
inclusive) that forms one complete, standalone narrative or thought — with a clear \
hook, a clean ending, and nothing important cut off mid-sentence. Prefer a total \
duration between {target_min} and {target_max} seconds; {max_s} seconds is a hard \
ceiling and {min_s} seconds a hard floor. End the clip right after the payoff line \
— never pad with irrelevant material to reach a target length, and never cut a \
thought short just to stay under one. A shorter clip is fine if that's genuinely \
what the content needs; anything over {max_s} seconds is rejected outright.

Segments may carry a speaker label like "(SPEAKER_01)" when the video has more \
than one speaker. Use it to keep straight who said what: a clip should be built \
around one speaker's thought, or a clear exchange between two, never a line \
lifted out of someone else's turn.

Frame the "hook", "seo_title", and "seo_description" fields around the IDEA the clip \
illustrates (discipline, patience, money, relationships, and so on) — not around the \
speaker's identity. The speaker is evidence supporting the lesson, not the star of the \
video; avoid framing like "[speaker] says this" unless the speaker's own identity is \
genuinely the point of the clip.

Respond with a single JSON array (no prose, no markdown fences), one object per \
window, in the same order the windows were given. Each object must have these \
fields:
- "window_index": integer, the N from that window's "Window N" label
- "start_segment": integer, index of the first segment to include (within that \
window's own numbering)
- "end_segment": integer, index of the last segment to include, inclusive
- "cold_open": either null, or an object {"start_segment": i, "end_segment": j} — a \
2-6 second range of segments from THIS SAME WINDOW (using this window's own \
numbering, not necessarily inside start_segment/end_segment) holding the single most \
striking line, usually the same line you'd pick for "pinned_line". Use this only \
when the natural opening of the chosen clip (start_segment) is weak but a strong \
line exists elsewhere in the window — it plays as a flash-forward BEFORE the main \
clip. Leave it null when the natural opening is already strong; don't force one. \
When you use a cold open, "hook_first_line" and "hook_pass" below must be judged on \
the cold open's own first line, not the main clip's natural opening.
- "scorecard": an object with these fields:
  - "hook_first_line": the exact first spoken sentence of the chosen clip, verbatim \
from the transcript (or of the cold open, if one is used)
  - "hook_pass": boolean, true only if that first line would stop a scroll in under \
a second on its own, with zero prior context
  - "hook_reason": one short sentence on why the first line would or wouldn't stop a \
scroll
  - "standalone": integer 0-10, how well the clip works with zero context from the \
rest of the video
  - "payoff": integer 0-10, strength of the punchline, payoff, or surprising claim
  - "lane_fit": integer 0-10, how well the clip fits the channel's lanes (see below)
  - {lane_field}
  - "loopability": integer 0-10, how naturally the clip's last line flows back into \
its first line, high if it could seamlessly re-loop
  - "overall": integer 0-10, overall quality of the clip as a standalone short
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
- "pinned_line": the single strongest VERBATIM (or lightly trimmed for grammar/ \
flow — never paraphrased or invented) sentence actually spoken within the chosen \
clip. Unlike "hook", this is a real quote from the transcript, not a tease — pick \
whichever line is most likely to be quoted or screenshotted on its own: the \
punchline, the most surprising claim, or the sharpest turn of phrase. Keep it to \
one sentence; trimming leading/trailing filler is fine, rewording is not.
- "pinned_line_emphasis": array of 1-3 words or short phrases, each an exact \
substring of "pinned_line", to visually highlight — the specific words carrying \
the line's weight (a number, a name, the key verb or claim). Empty array only if \
truly nothing in the line stands out.
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

Scorecard rules:
- hook_pass = false if the first line starts mid-thought ("So the…", "And that's \
why…", "Yeah."), needs earlier context to make sense, is filler or a greeting, or \
makes no claim and creates no tension. A pass looks like a direct claim, a question, \
a number, or something said straight to the viewer ("Most people never…", "Nobody \
tells you…", "You're not lazy, you're…").
- {lane_rule}
- Loopability: score high when the last line flows naturally back into the first, \
so the clip could replay seamlessly.
"""


def system_prompt(lengths: ClipLength = SHORT) -> str:
    """SYSTEM_PROMPT with this job's clip lengths and the channel settings
    filled in. Plain replace, since the prompt's JSON examples contain
    literal braces."""
    def fmt(v: float) -> str:
        return f"{v:g}"

    return (SYSTEM_PROMPT
            .replace("{target_min}", fmt(lengths.target_min_s))
            .replace("{target_max}", fmt(lengths.target_max_s))
            .replace("{max_s}", fmt(lengths.max_s))
            .replace("{min_s}", fmt(lengths.min_s))
            .replace("{channel}", channel.describe())
            .replace("{lane_field}", channel.lane_field())
            .replace("{lane_rule}", channel.lane_rule()))


def build_windows(
    segments: list[dict],
    min_len: float = 20.0,
    max_len: float = 60.0,
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


# Per-window token budget for score_batch. Output includes a variable-size
# extended-thinking block ahead of the actual JSON (observed 600-2000+
# thinking tokens per window) sharing the same max_tokens ceiling — sized
# with generous headroom above the actual JSON object's own needs, since a
# too-tight budget causes the response to truncate mid-JSON rather than
# finish early (confirmed: ~1 in 5 batches truncated at the previous 1024
# budget once pinned_line/pinned_line_emphasis were added to the schema; the
# nested "scorecard" object added enough
# fields that even 2048 started truncating every batch, so this has more
# headroom than that field count alone would suggest).
TOKENS_PER_WINDOW = 4096

_feedback_block_logged = False


def _build_feedback_block() -> str:
    """Retention upgrade plan phase 5: once there's enough real posted-clip
    data, tell the model what has actually worked/flopped on THIS channel —
    the top 5 and bottom 5 hooks by stayed_to_watch_pct. Deliberately built
    into the per-call USER message rather than SYSTEM_PROMPT: the system
    block above is marked cache_control=ephemeral, and this block changes
    as new performance data comes in, so putting it there would bust that
    cache on every call instead of just once per day/deploy."""
    global _feedback_block_logged
    records = [r for r in performance.load_records() if r.get("stayed_to_watch_pct") is not None]
    if len(records) < performance.MIN_RECORDS_FOR_FEEDBACK:
        return ""

    records.sort(key=lambda r: r["stayed_to_watch_pct"], reverse=True)
    top5, bottom5 = records[:5], records[-5:]

    lines = [
        "What works on this channel (real posted-clip performance — a signal to "
        "weigh, not a hard rule):",
        "Top performers by stayed-to-watch %:",
    ]
    lines += [f'  - "{r.get("hook", "")}" — {r["stayed_to_watch_pct"]:.0f}%' for r in top5]
    lines.append("Lowest performers by stayed-to-watch %:")
    lines += [f'  - "{r.get("hook", "")}" — {r["stayed_to_watch_pct"]:.0f}%' for r in bottom5]

    if not _feedback_block_logged:
        print(f"[info] including performance feedback block in prompt ({len(records)} records with stayed_to_watch_pct)")
        _feedback_block_logged = True

    return "\n".join(lines) + "\n\n"


def _call_and_parse(client: Anthropic, batch: list[dict], prompt: str, lengths: ClipLength = SHORT) -> list[dict]:
    feedback_block = _build_feedback_block()
    message = client.messages.create(
        model=MODEL,
        max_tokens=TOKENS_PER_WINDOW * len(batch),
        system=[{
            "type": "text",
            "text": system_prompt(lengths),
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": f"{feedback_block}Candidate windows:\n\n{prompt}"}],
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
    if message.stop_reason == "max_tokens":
        raise RuntimeError(f"response truncated at max_tokens ({TOKENS_PER_WINDOW * len(batch)})")
    results_raw = json.loads(raw)
    if not isinstance(results_raw, list):
        raise ValueError("expected a JSON array of per-window results")
    return results_raw


def score_batch(client: Anthropic, batch: list[dict], lengths: ClipLength = SHORT) -> list[dict]:
    parts = []
    for i, window in enumerate(batch):
        segs = window["segments"]
        numbered = "\n".join(
            f"[{j}] {s['start']:.1f}s-{s['end']:.1f}s{diarize.label(s)}: {s['text'].strip()}"
            for j, s in enumerate(segs)
        )
        parts.append(f"Window {i}:\n{numbered}")
    prompt = "\n\n".join(parts)

    # A malformed/truncated JSON response is rare but not deterministic
    # (observed ~1 in 5 with extended thinking's variable token usage) — one
    # retry recovers the whole batch instead of silently dropping every
    # window in it, which previously meant a short video's only batch
    # failing could produce zero clips end to end.
    try:
        results_raw = _call_and_parse(client, batch, prompt, lengths)
    except (json.JSONDecodeError, RuntimeError, ValueError) as e:
        print(f"  [warn] batch response failed to parse ({e}), retrying once")
        results_raw = _call_and_parse(client, batch, prompt, lengths)

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
            sc_raw = result["scorecard"]
            scorecard = {
                "hook_first_line": sc_raw.get("hook_first_line", ""),
                "hook_pass": bool(sc_raw["hook_pass"]),
                "hook_reason": sc_raw.get("hook_reason", ""),
                "standalone": float(sc_raw.get("standalone", 0)),
                "payoff": float(sc_raw.get("payoff", 0)),
                "lane_fit": float(sc_raw.get("lane_fit", 0)),
                "lane": sc_raw.get("lane", "other"),
                "loopability": float(sc_raw.get("loopability", 0)),
                "overall": float(sc_raw.get("overall", 0)),
            }
        except (KeyError, ValueError, TypeError) as e:
            print(f"  [warn] bad result for window [{window['start']:.1f}-{window['end']:.1f}]: {e}")
            continue
        chosen = segs[start_idx : end_idx + 1]
        duration = chosen[-1]["end"] - chosen[0]["start"]
        if duration > lengths.max_s or duration < lengths.min_s:
            print(f"  [warn] rejecting clip [{chosen[0]['start']:.1f}-{chosen[-1]['end']:.1f}] "
                  f"({duration:.1f}s, outside {lengths.min_s:g}-{lengths.max_s:g}s)")
            continue

        cold_open = None
        cold_open_raw = result.get("cold_open")
        if cold_open_raw:
            try:
                co_start_idx = int(cold_open_raw["start_segment"])
                co_end_idx = int(cold_open_raw["end_segment"])
                if not (0 <= co_start_idx <= co_end_idx < len(segs)):
                    raise ValueError(f"cold_open segment indices {co_start_idx}-{co_end_idx} out of range for {len(segs)} segments")
                co_segs = segs[co_start_idx : co_end_idx + 1]
                co_duration = co_segs[-1]["end"] - co_segs[0]["start"]
                if not (2.0 <= co_duration <= 6.0):
                    raise ValueError(f"cold_open duration {co_duration:.1f}s outside 2-6s")
                cold_open = {
                    "start": co_segs[0]["start"],
                    "end": co_segs[-1]["end"],
                    "text": " ".join(s["text"].strip() for s in co_segs),
                }
            except (KeyError, ValueError, TypeError) as e:
                print(f"  [warn] dropping cold_open for clip [{chosen[0]['start']:.1f}-{chosen[-1]['end']:.1f}]: {e}")

        out.append({
            "start": chosen[0]["start"],
            "end": chosen[-1]["end"],
            "text": " ".join(s["text"].strip() for s in chosen),
            "cold_open": cold_open,
            "scorecard": scorecard,
            "emphasis_words": result.get("emphasis_words", []),
            "hook": result.get("hook", ""),
            "pinned_line": result.get("pinned_line", ""),
            "pinned_line_emphasis": result.get("pinned_line_emphasis", []),
            "seo_title": result.get("seo_title", ""),
            "seo_description": result.get("seo_description", ""),
        })
    return out


def score_all_windows(
    segments: list[dict],
    energy: Optional[dict] = None,
    energy_percentile: float = 10.0,
    lengths: ClipLength = SHORT,
    **window_kwargs,
) -> list[dict]:
    client = Anthropic()
    window_kwargs.setdefault("max_len", lengths.window_len())
    windows = build_windows(segments, **window_kwargs)
    if energy is not None:
        windows = filter_low_energy(windows, energy, energy_percentile)

    batches = list(_chunk(windows, SCORING_BATCH_SIZE))

    scored = []
    with ThreadPoolExecutor(max_workers=SCORING_CONCURRENCY) as executor:
        future_to_batch = {executor.submit(score_batch, client, b, lengths): b for b in batches}
        for future in as_completed(future_to_batch):
            b = future_to_batch[future]
            try:
                scored.extend(future.result())
            except Exception as e:
                print(f"  [warn] scoring failed for batch starting [{b[0]['start']:.1f}s]: {e}")
    return scored


if __name__ == "__main__":
    import sys

    from clippers.audio_energy import analyze_energy
    from clippers.pipeline import transcribe

    path = sys.argv[1] if len(sys.argv) > 1 else "downloads/lgaR7Z9okYM.mp4"
    print(f"Transcribing {path} ...")
    segments = transcribe(path, model_size="small")

    print(f"Scoring {len(segments)} segments' worth of sliding windows via Claude ...")
    energy = analyze_energy(path)
    scored = score_all_windows(segments, energy=energy)

    scored.sort(key=lambda c: c["scorecard"]["overall"], reverse=True)
    for c in scored:
        sc = c["scorecard"]
        pass_tag = "PASS" if sc["hook_pass"] else "FAIL"
        print(f"[{c['start']:6.1f}-{c['end']:6.1f}] overall={sc['overall']:.1f} hook={pass_tag} lane={sc['lane']}  {sc['hook_reason']}")
        if c["cold_open"]:
            co = c["cold_open"]
            print(f"           cold open [{co['start']:.1f}-{co['end']:.1f}]: {co['text']!r}")
        if c["emphasis_words"]:
            print(f"           emphasis: {c['emphasis_words']}")
