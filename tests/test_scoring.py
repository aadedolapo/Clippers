import numpy as np
import pytest

from clippers import llm_scoring, scoring
from clippers.llm_scoring import build_windows, filter_low_energy
from conftest import seg


def test_build_windows_tiles_video_without_gaps():
    segments = [seg(t, t + 5) for t in range(0, 300, 5)]
    windows = build_windows(segments)
    assert windows[0]["start"] == 0.0
    covered_to = 0.0
    for w in windows:
        assert w["start"] <= covered_to, "gap between windows"
        covered_to = max(covered_to, w["end"])
        assert w["segments"], "every window carries its segments"
    assert covered_to == 300


def test_build_windows_widens_stride_for_long_videos():
    segments = [seg(t, t + 5) for t in range(0, 20000, 5)]
    windows = build_windows(segments, target_windows=120)
    # Stride caps at max_len, so the count stays near the target.
    assert len(windows) <= 400


def test_build_windows_empty():
    assert build_windows([]) == []


def test_filter_low_energy_drops_quietest():
    windows = [{"start": float(i * 10), "end": float(i * 10 + 10)} for i in range(10)]
    times = np.arange(0, 100, 0.5)
    combined = (times // 10) / 10.0  # window i has energy i/10
    energy = {"times": times, "combined": combined}
    kept = filter_low_energy(windows, energy, percentile=10.0)
    assert windows[0] not in kept
    assert len(kept) == 9


def test_overlaps():
    a = {"start": 0, "end": 10}
    assert scoring._overlaps(a, {"start": 5, "end": 15})
    assert not scoring._overlaps(a, {"start": 10, "end": 20})


def test_weights_sum_to_one():
    assert sum(scoring.WEIGHTS.values()) == pytest.approx(1.0)


def test_combined_score_neutral_visual_and_bounds():
    sc = {"hook_pass": True, "payoff": 10, "standalone": 10, "lane_fit": 10, "loopability": 10}
    assert scoring.combined_score(sc, 1.0, 1.0) == pytest.approx(1.0)
    neutral = scoring.combined_score(sc, 1.0)
    assert neutral == pytest.approx(1.0 - scoring.WEIGHTS["visual"] * 0.5)
    failed = {**sc, "hook_pass": False}
    assert scoring.combined_score(failed, 1.0, 1.0) == pytest.approx(1.0 - scoring.WEIGHTS["hook"])


def test_system_prompt_fills_lengths_and_keeps_json_braces():
    p = llm_scoring.system_prompt(llm_scoring.LONG)
    assert "{max_s}" not in p and "{target_min}" not in p
    assert f"{llm_scoring.LONG.max_s:g} seconds is a hard" in p
    assert '{"start_segment": i' in p
