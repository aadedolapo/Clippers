"""
Orchestrator agent: replaces the fixed "score -> take top 3" step with a
Claude tool-use loop that reviews the scored candidates, re-reads the
transcript around them, fixes clip boundaries, and decides how many clips
(if any) are actually worth posting.

Scoring itself (llm_scoring + audio energy via scoring.score_video) still
runs first and unchanged — it's the cheap, parallel candidate generator.
The agent's job is the editorial judgment on top of it. It never touches
Resolve: sending to Resolve stays a human step in the UI.
"""

import functools
import json
import logging
import os
from typing import Callable, Optional

from dotenv import load_dotenv
from anthropic import Anthropic, beta_tool

from llm_scoring import _build_feedback_block
from scoring import score_video

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logger = logging.getLogger("viral_clipper.agent")

MODEL = os.environ.get("CLAUDE_AGENT_MODEL", "claude-opus-5-5")
# How wide a candidate pool the agent reviews, and the most it may pick.
CANDIDATE_POOL = 10
MAX_PICKS = 6
# Hard cap on model turns so a confused run can't loop forever.
MAX_ITERATIONS = 40
# Same bounds llm_scoring.score_batch enforces on its own picks.
MIN_CLIP_SECONDS = 15.0
MAX_CLIP_SECONDS = 45.0
MAX_TRANSCRIPT_SPAN = 240.0
# Fallback when the agent run itself fails: the old behavior.
FALLBACK_TOP_N = 3

SYSTEM_PROMPT = """You are the editor for a short-form channel (YouTube Shorts / \
Reels / TikTok) whose lanes are faith, men's growth, discipline, and purpose. \
You are given candidate clips from one long video. An automated scorer already \
proposed them; your job is to decide which ones are genuinely worth posting \
and to make each one as strong as possible before a human reviews it.

For each candidate you take seriously:
- Use read_transcript to look at what comes just before and after it.
- Check that its first line is a complete sentence that stops a scroll with zero \
prior context (not "So...", "And that's why...", a greeting, or a reference to \
something said earlier). If a stronger opening line sits a few seconds earlier \
or later, move the start with adjust_clip.
- Check that it ends right after the payoff — not mid-thought, and not padded \
with whatever came next. Fix the end with adjust_clip if needed.
- Clips must stay between 15 and 45 seconds; 20-35 is the sweet spot.

Then pick with select_clip, best first. Quality over quantity: pick only clips \
you would actually post. Picking one or two is normal; picking none is a valid \
answer for a weak video. Don't pick two clips that make the same point. Never \
pick more than {max_picks}.

When you're done, reply with a short plain-text summary of what you picked and \
why, and anything you rejected that a human might want to double-check."""


def _fmt_time(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}"


def _snap(segments: list[dict], start: float, end: float) -> tuple[float, float, list[dict]]:
    """Snap a requested range to whole transcript segments, so clips always
    start and end on segment boundaries like the scorer's own picks do."""
    start_seg = min(segments, key=lambda s: abs(s["start"] - start))
    end_seg = min(segments, key=lambda s: abs(s["end"] - end))
    chosen = [s for s in segments if s["start"] >= start_seg["start"] and s["end"] <= end_seg["end"]]
    if not chosen:
        raise ValueError("no transcript segments in that range")
    return chosen[0]["start"], chosen[-1]["end"], chosen


def _describe_candidate(cid: int, c: dict) -> str:
    sc = c["scorecard"]
    lines = [
        f"Candidate {cid}: {_fmt_time(c['start'])}-{_fmt_time(c['end'])} "
        f"({c['start']:.1f}s-{c['end']:.1f}s, {c['end'] - c['start']:.1f}s long)",
        f"  score={c['combined_score']:.2f} lane={sc['lane']} standalone={sc['standalone']:.0f} "
        f"payoff={sc['payoff']:.0f} loopability={sc['loopability']:.0f}",
        f"  first line: {sc['hook_first_line']!r} ({sc['hook_reason']})",
        f"  on-screen hook: {c['hook']!r}",
    ]
    if c.get("cold_open"):
        co = c["cold_open"]
        lines.append(f"  cold open {co['start']:.1f}s-{co['end']:.1f}s: {co['text']!r}")
    lines.append(f"  text: {c['text']}")
    return "\n".join(lines)


def run_agent(
    segments: list[dict],
    filepath: str,
    title: str,
    source: Optional[str] = None,
    on_event: Optional[Callable[[str, str], None]] = None,
) -> tuple[list[dict], list[dict], str]:
    """Returns (selected, rejected, summary) — the same (selected, rejected)
    shape as scoring.score_video, plus the agent's plain-text summary. Each
    selected clip gets an "agent_note" with the reason it was picked.

    on_event(kind, text) is called as the run progresses (kinds: stage,
    thinking, say, tool, result, error) so a UI can show the agent at work."""

    def emit(kind: str, text: str) -> None:
        if on_event is not None:
            try:
                on_event(kind, text)
            except Exception:
                logger.exception("on_event callback failed")

    def logged(fn):
        """Report each tool call and its result through emit()."""
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            emit("tool", f"{fn.__name__}({', '.join(f'{k}={v!r}' for k, v in kwargs.items())})")
            result = fn(*args, **kwargs)
            emit("result", result)
            return result
        return wrapper

    emit("stage", "Scoring transcript windows for candidate clips")
    candidates, rejected = score_video(segments, filepath, top_n=CANDIDATE_POOL)
    emit("stage", f"Scorer proposed {len(candidates)} candidates ({len(rejected)} failed the hook check); handing them to the agent")
    if not candidates:
        return [], rejected, "The scorer found no candidates that passed the hook check."

    # Work on copies so a failed run's half-applied edits never leak into the
    # fallback result.
    pool = {i: dict(c) for i, c in enumerate(candidates)}
    picks: list[int] = []
    notes: dict[int, str] = {}

    @beta_tool
    @logged
    def read_transcript(start: float, end: float) -> str:
        """Read the timestamped transcript between two times in the source video.

        Args:
            start: Start time in seconds.
            end: End time in seconds. At most 240 seconds after start.
        """
        if end <= start:
            return "Error: end must be after start."
        end = min(end, start + MAX_TRANSCRIPT_SPAN)
        rows = [
            f"{s['start']:.1f}s-{s['end']:.1f}s: {s['text'].strip()}"
            for s in segments
            if s["end"] > start and s["start"] < end
        ]
        return "\n".join(rows) or "No speech in that range."

    @beta_tool
    @logged
    def adjust_clip(candidate_id: int, start: float, end: float) -> str:
        """Change a candidate's start and/or end time. Times snap to the nearest
        transcript segment boundary. The result must be 15-45 seconds long.

        Args:
            candidate_id: The candidate's number.
            start: New start time in seconds.
            end: New end time in seconds.
        """
        c = pool.get(candidate_id)
        if c is None:
            return f"Error: no candidate {candidate_id}."
        try:
            new_start, new_end, chosen = _snap(segments, start, end)
        except ValueError as e:
            return f"Error: {e}."
        duration = new_end - new_start
        if not (MIN_CLIP_SECONDS <= duration <= MAX_CLIP_SECONDS):
            return f"Error: that range snaps to {new_start:.1f}s-{new_end:.1f}s ({duration:.1f}s), outside 15-45s. Unchanged."
        others = [o for i, o in pool.items() if i != candidate_id and i in picks]
        if any(new_start < o["end"] and o["start"] < new_end for o in others):
            return "Error: that range overlaps a clip you've already selected. Unchanged."
        c["start"], c["end"] = new_start, new_end
        c["text"] = " ".join(s["text"].strip() for s in chosen)
        first_line = chosen[0]["text"].strip()
        c["scorecard"] = {**c["scorecard"], "hook_first_line": first_line}
        # Caption extras chosen for the old range may no longer be spoken in
        # the new one; drop them rather than render text that isn't there.
        lowered = c["text"].lower()
        c["emphasis_words"] = [w for w in c.get("emphasis_words", []) if w.lower() in lowered]
        if c.get("pinned_line") and c["pinned_line"].lower() not in lowered:
            c["pinned_line"] = ""
            c["pinned_line_emphasis"] = []
        c["agent_adjusted"] = True
        return (
            f"Candidate {candidate_id} is now {new_start:.1f}s-{new_end:.1f}s ({duration:.1f}s). "
            f"First line: {first_line!r}. Last line: {chosen[-1]['text'].strip()!r}."
        )

    @beta_tool
    @logged
    def select_clip(candidate_id: int, reason: str) -> str:
        """Pick a candidate for the human review queue. Call once per clip,
        best clip first.

        Args:
            candidate_id: The candidate's number.
            reason: One sentence on why this clip is worth posting.
        """
        if candidate_id not in pool:
            return f"Error: no candidate {candidate_id}."
        if candidate_id in picks:
            return f"Candidate {candidate_id} is already selected."
        if len(picks) >= MAX_PICKS:
            return f"Error: already at the {MAX_PICKS}-clip limit."
        c = pool[candidate_id]
        for i in picks:
            o = pool[i]
            if c["start"] < o["end"] and o["start"] < c["end"]:
                return f"Error: overlaps selected candidate {i}."
        picks.append(candidate_id)
        notes[candidate_id] = reason
        return f"Selected candidate {candidate_id} ({len(picks)} picked so far)."

    feedback = _build_feedback_block()
    user_msg = (
        f"Video: {title}\n"
        + (f"Source: {source}\n" if source else "")
        + f"Video length: {segments[-1]['end']:.0f}s\n\n"
        + feedback
        + "Candidates:\n\n"
        + "\n\n".join(_describe_candidate(i, c) for i, c in pool.items())
    )

    client = Anthropic()
    summary = ""
    try:
        runner = client.beta.messages.tool_runner(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT.format(max_picks=MAX_PICKS),
            tools=[read_transcript, adjust_clip, select_clip],
            messages=[{"role": "user", "content": user_msg}],
            output_config={"effort": "high"},
            # Readable summaries of the agent's reasoning, for the live view.
            thinking={"type": "adaptive", "display": "summarized"},
            max_iterations=MAX_ITERATIONS,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        last = None
        for message in runner:
            last = message
            for block in message.content:
                if block.type == "thinking" and block.thinking.strip():
                    emit("thinking", block.thinking.strip())
                elif block.type == "text" and block.text.strip():
                    emit("say", block.text.strip())
                elif block.type == "tool_use":
                    logger.info(f"agent -> {block.name}({json.dumps(block.input)})")
        if last is None:
            raise RuntimeError("agent produced no response")
        if last.stop_reason == "refusal":
            raise RuntimeError("agent request was refused")
        summary = "\n".join(b.text for b in last.content if b.type == "text").strip()
    except Exception as e:
        logger.exception("agent run failed; falling back to top-scored candidates")
        emit("error", f"Agent failed: {e}. Falling back to the top {FALLBACK_TOP_N} scored clips.")
        fallback = candidates[:FALLBACK_TOP_N]
        return fallback, rejected, f"Agent failed ({e}); showing the top {len(fallback)} scored clips instead."

    selected = []
    for i in picks:
        c = pool[i]
        c["agent_note"] = notes[i]
        selected.append(c)
    emit("stage", f"Agent finished: {len(selected)} clip(s) picked")
    return selected, rejected, summary


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from pipeline import transcribe

    path = sys.argv[1]
    print(f"Transcribing {path} ...")
    segs = transcribe(path)
    selected, _, summary = run_agent(
        segs, path, title=os.path.splitext(os.path.basename(path))[0],
        on_event=lambda kind, text: print(f"[{kind}] {text}", flush=True),
    )
    print(f"\n{len(selected)} picked:\n")
    for c in selected:
        adj = " (adjusted)" if c.get("agent_adjusted") else ""
        print(f"[{c['start']:6.1f}-{c['end']:6.1f}]{adj} {c['agent_note']}")
        print(f"    first line: {c['scorecard']['hook_first_line']!r}")
    print(f"\nSummary:\n{summary}")
