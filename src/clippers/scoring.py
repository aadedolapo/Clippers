"""
Combine the LLM scoring pass and audio energy pass into a final ranked,
non-overlapping list of candidate clips (PRD section 4, "Combine").
"""

from typing import Optional

from clippers import shots, visual_rerank
from clippers.audio_energy import analyze_energy, window_energy_score
from clippers.llm_scoring import SHORT, ClipLength, score_all_windows

# Retention-focused combined score (retention upgrade plan, phase 2): the
# hook is weighted highest since a weak first line loses the viewer before
# anything else about the clip matters. Audio energy (originally 30%) is
# nearly dropped — loudness doesn't predict retention the way a strong hook
# does. Weights sum to 1.0; each subterm is normalized to 0-1 before
# weighting (the 0-10 LLM scorecard fields divided by 10, hook_pass treated
# as 10/10 or 0/10, audio_score already 0-1) so combined_score lands in 0-1.
# "visual" (improvement plan #5) is the visual rerank's 0-1 score; the other
# weights were scaled by 0.9 to make room for it. Check any change here with
# `python eval/run_eval.py --weights-only` before trusting it.
WEIGHTS = {
    "hook": 0.315,
    "payoff": 0.18,
    "standalone": 0.135,
    "lane_fit": 0.135,
    "loopability": 0.09,
    "audio_energy": 0.045,
    "visual": 0.1,
}

# How many of the best text-scored candidates get the visual rerank call.
VISUAL_SHORTLIST_MIN = 6


def combined_score(scorecard: dict, audio_score: float, visual_score: Optional[float] = None) -> float:
    """The weighted 0-1 score. visual_score None (not reranked) counts as
    neutral, so unreviewed candidates aren't penalized for it."""
    if visual_score is None:
        visual_score = visual_rerank.NEUTRAL_SCORE
    hook_term = 1.0 if scorecard["hook_pass"] else 0.0
    return (
        WEIGHTS["hook"] * hook_term
        + WEIGHTS["payoff"] * (scorecard["payoff"] / 10.0)
        + WEIGHTS["standalone"] * (scorecard["standalone"] / 10.0)
        + WEIGHTS["lane_fit"] * (scorecard["lane_fit"] / 10.0)
        + WEIGHTS["loopability"] * (scorecard["loopability"] / 10.0)
        + WEIGHTS["audio_energy"] * audio_score
        + WEIGHTS["visual"] * visual_score
    )


def _visual_value(w: dict) -> Optional[float]:
    return w["visual"]["score"] if w.get("visual") else None


def _pick_non_overlapping(ranked: list[dict], n: int) -> list[dict]:
    picked: list[dict] = []
    for w in ranked:
        if any(_overlaps(w, s) for s in picked):
            continue
        picked.append(w)
        if len(picked) >= n:
            break
    return picked


def score_video(
    segments: list[dict],
    audio_path: str,
    top_n: int = 5,
    shot_cuts: Optional[list[float]] = None,
    lengths: ClipLength = SHORT,
) -> tuple[list[dict], list[dict]]:
    """Returns (selected, rejected). `rejected` holds every clip whose
    scorecard failed the hook check (scoring.py never lets a hook_pass=false
    clip into the final list) so the UI can still show why a candidate was
    dropped instead of silently discarding it.

    shot_cuts (shots.detect_shots) moves each candidate off shot slivers at
    its edges. audio_path is the source video, also used to grab frames for
    the visual rerank of the best text-scored candidates."""
    energy = analyze_energy(audio_path)
    llm_windows = score_all_windows(segments, energy=energy, lengths=lengths)

    for w in llm_windows:
        if shot_cuts:
            shots.snap_clip(w, shot_cuts, segments)
        audio_score = window_energy_score(energy, w["start"], w["end"])
        w["audio_score"] = audio_score
        sc = w["scorecard"]
        w["combined_score"] = combined_score(sc, audio_score)
        # Backward compatibility: the frontend and saved_clips.json still
        # read llm_score/reason directly.
        w["llm_score"] = sc["overall"] / 10.0
        w["reason"] = sc["hook_reason"]
        w["reject_reason"] = sc["hook_reason"]

    passing = [w for w in llm_windows if w["scorecard"]["hook_pass"]]
    rejected = [w for w in llm_windows if not w["scorecard"]["hook_pass"]]
    passing.sort(key=lambda c: c["combined_score"], reverse=True)
    rejected.sort(key=lambda c: c["combined_score"], reverse=True)

    # Visual rerank on a shortlist twice the final size, so a candidate just
    # outside the text top N can still climb in on visuals.
    shortlist = _pick_non_overlapping(passing, max(VISUAL_SHORTLIST_MIN, top_n * 2))
    visual_rerank.rerank(audio_path, shortlist)
    for w in shortlist:
        w["combined_score"] = combined_score(w["scorecard"], w["audio_score"], _visual_value(w))
    passing.sort(key=lambda c: c["combined_score"], reverse=True)

    selected = _pick_non_overlapping(passing, top_n)

    selected.sort(key=lambda c: c["combined_score"], reverse=True)
    return selected, rejected


def _overlaps(a: dict, b: dict) -> bool:
    return a["start"] < b["end"] and b["start"] < a["end"]


if __name__ == "__main__":
    import sys

    from clippers.pipeline import transcribe

    path = sys.argv[1] if len(sys.argv) > 1 else "downloads/lgaR7Z9okYM.mp4"
    print(f"Transcribing {path} ...")
    segments = transcribe(path, model_size="small")

    print("Scoring (LLM + audio energy) ...")
    clips, rejected = score_video(segments, path, top_n=5)

    print(f"\n{len(clips)} candidate clips:\n")
    for c in clips:
        duration = c["end"] - c["start"]
        sc = c["scorecard"]
        print(
            f"[{c['start']:6.1f}-{c['end']:6.1f}] ({duration:4.1f}s)  "
            f"combined={c['combined_score']:.2f}  lane={sc['lane']}  audio={c['audio_score']:.2f}"
        )
        print(f"    hook: {sc['hook_first_line']!r} ({sc['hook_reason']})")
        if c["emphasis_words"]:
            print(f"    emphasis: {c['emphasis_words']}")

    print(f"\n{len(rejected)} hook-fail rejects:\n")
    for c in rejected:
        sc = c["scorecard"]
        print(f"[{c['start']:6.1f}-{c['end']:6.1f}] hook: {sc['hook_first_line']!r} ({sc['hook_reason']})")
