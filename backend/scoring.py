"""
Combine the LLM scoring pass and audio energy pass into a final ranked,
non-overlapping list of candidate clips (PRD section 4, "Combine").
"""

from audio_energy import analyze_energy, window_energy_score
from llm_scoring import score_all_windows

LLM_WEIGHT = 0.7
AUDIO_WEIGHT = 0.3


def score_video(segments: list[dict], audio_path: str, top_n: int = 5) -> list[dict]:
    energy = analyze_energy(audio_path)
    llm_windows = score_all_windows(segments, energy=energy)

    for w in llm_windows:
        audio_score = window_energy_score(energy, w["start"], w["end"])
        w["audio_score"] = audio_score
        w["combined_score"] = LLM_WEIGHT * w["llm_score"] + AUDIO_WEIGHT * audio_score

    llm_windows.sort(key=lambda c: c["combined_score"], reverse=True)

    selected: list[dict] = []
    for w in llm_windows:
        if any(_overlaps(w, s) for s in selected):
            continue
        selected.append(w)
        if len(selected) >= top_n:
            break

    selected.sort(key=lambda c: c["combined_score"], reverse=True)
    return selected


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
    clips = score_video(segments, path, top_n=5)

    print(f"\n{len(clips)} candidate clips:\n")
    for c in clips:
        duration = c["end"] - c["start"]
        print(
            f"[{c['start']:6.1f}-{c['end']:6.1f}] ({duration:4.1f}s)  "
            f"combined={c['combined_score']:.2f}  llm={c['llm_score']:.2f}  audio={c['audio_score']:.2f}"
        )
        print(f"    reason: {c['reason']}")
        if c["emphasis_words"]:
            print(f"    emphasis: {c['emphasis_words']}")
