"""
Combine the LLM scoring pass and audio energy pass into a final ranked,
non-overlapping list of candidate clips (PRD section 4, "Combine").
"""

from audio_energy import analyze_energy, window_energy_score
from llm_scoring import score_all_windows

# Retention-focused combined score (retention upgrade plan, phase 2): the
# hook is weighted highest since a weak first line loses the viewer before
# anything else about the clip matters. Audio energy (originally 30%) is
# nearly dropped — loudness doesn't predict retention the way a strong hook
# does. Weights sum to 1.0; each subterm is normalized to 0-1 before
# weighting (the 0-10 LLM scorecard fields divided by 10, hook_pass treated
# as 10/10 or 0/10, audio_score already 0-1) so combined_score lands in 0-1.
WEIGHTS = {
    "hook": 0.35,
    "payoff": 0.2,
    "standalone": 0.15,
    "lane_fit": 0.15,
    "loopability": 0.1,
    "audio_energy": 0.05,
}


def score_video(segments: list[dict], audio_path: str, top_n: int = 5) -> tuple[list[dict], list[dict]]:
    """Returns (selected, rejected). `rejected` holds every clip whose
    scorecard failed the hook check (scoring.py never lets a hook_pass=false
    clip into the final list) so the UI can still show why a candidate was
    dropped instead of silently discarding it."""
    energy = analyze_energy(audio_path)
    llm_windows = score_all_windows(segments, energy=energy)

    for w in llm_windows:
        audio_score = window_energy_score(energy, w["start"], w["end"])
        w["audio_score"] = audio_score
        sc = w["scorecard"]
        hook_term = 1.0 if sc["hook_pass"] else 0.0
        w["combined_score"] = (
            WEIGHTS["hook"] * hook_term
            + WEIGHTS["payoff"] * (sc["payoff"] / 10.0)
            + WEIGHTS["standalone"] * (sc["standalone"] / 10.0)
            + WEIGHTS["lane_fit"] * (sc["lane_fit"] / 10.0)
            + WEIGHTS["loopability"] * (sc["loopability"] / 10.0)
            + WEIGHTS["audio_energy"] * audio_score
        )
        # Backward compatibility: the frontend and saved_clips.json still
        # read llm_score/reason directly.
        w["llm_score"] = sc["overall"] / 10.0
        w["reason"] = sc["hook_reason"]
        w["reject_reason"] = sc["hook_reason"]

    passing = [w for w in llm_windows if w["scorecard"]["hook_pass"]]
    rejected = [w for w in llm_windows if not w["scorecard"]["hook_pass"]]
    passing.sort(key=lambda c: c["combined_score"], reverse=True)
    rejected.sort(key=lambda c: c["combined_score"], reverse=True)

    selected: list[dict] = []
    for w in passing:
        if any(_overlaps(w, s) for s in selected):
            continue
        selected.append(w)
        if len(selected) >= top_n:
            break

    selected.sort(key=lambda c: c["combined_score"], reverse=True)
    return selected, rejected


def _overlaps(a: dict, b: dict) -> bool:
    return a["start"] < b["end"] and b["start"] < a["end"]


if __name__ == "__main__":
    import sys
    import os

    sys.path.insert(0, os.path.dirname(__file__))
    from pipeline import transcribe

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
