"""
Audio energy analysis (PRD section 4B): RMS energy + onset/beat spikes as a
secondary, cheap signal that correlates with laughs, reactions, punchlines.
"""

import librosa
import numpy as np


def analyze_energy(audio_path: str, hop_length: int = 512) -> dict:
    """Load audio and compute a per-frame energy signal plus onset strength."""
    y, sr = librosa.load(audio_path, sr=None, mono=True)

    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop_length)

    frame_times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop_length)

    # Normalize each signal independently to 0-1, then combine.
    def normalize(arr: np.ndarray) -> np.ndarray:
        lo, hi = arr.min(), arr.max()
        if hi - lo < 1e-9:
            return np.zeros_like(arr)
        return (arr - lo) / (hi - lo)

    rms_norm = normalize(rms)
    onset_norm = normalize(onset_env[: len(rms_norm)])

    combined = 0.5 * rms_norm + 0.5 * onset_norm

    return {
        "times": frame_times,
        "rms": rms_norm,
        "onset": onset_norm,
        "combined": combined,
        "duration": float(len(y) / sr),
    }


def window_energy_score(energy: dict, start: float, end: float) -> float:
    """Average the combined energy signal within [start, end], 0-1."""
    times = energy["times"]
    mask = (times >= start) & (times < end)
    if not mask.any():
        return 0.0
    return float(energy["combined"][mask].mean())


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "downloads/lgaR7Z9okYM.mp4"
    energy = analyze_energy(path)
    print(f"Duration: {energy['duration']:.2f}s, {len(energy['times'])} frames")

    # Print energy for consecutive 10s windows as a sanity check.
    for start in np.arange(0, energy["duration"], 10):
        score = window_energy_score(energy, start, start + 10)
        bar = "#" * int(score * 40)
        print(f"[{start:6.1f}s] {score:.3f} {bar}")
