from shots import snap_clip, snap_to_shots


def w(a, b):
    return {"word": "x", "start": a, "end": b}


def test_cut_just_before_start_pulls_start_back():
    assert snap_to_shots(10.0, 30.0, [9.7]) == (9.7, 30.0)


def test_cut_just_after_start_with_no_speech_yet_moves_start_forward():
    words = [w(10.4, 10.8)]
    assert snap_to_shots(10.0, 30.0, [10.35], words) == (10.35, 30.0)


def test_cut_after_speech_started_backs_off_to_earlier_cut():
    words = [w(10.0, 10.5), w(10.5, 11.0)]
    assert snap_to_shots(10.0, 30.0, [9.0, 10.3], words) == (9.0, 30.0)


def test_cut_after_speech_started_without_clean_earlier_cut_takes_the_cut():
    words = [w(9.2, 9.8), w(10.0, 10.5)]
    # Earlier cut at 9.0 has speech between it and the start, so no backoff.
    assert snap_to_shots(10.0, 30.0, [9.0, 10.3], words) == (10.3, 30.0)


def test_end_never_lands_just_after_a_cut():
    assert snap_to_shots(10.0, 30.0, [29.9]) == (10.0, 29.9)
    assert snap_to_shots(10.0, 30.0, [29.5]) == (10.0, 30.0)


def test_far_cuts_change_nothing():
    assert snap_to_shots(10.0, 30.0, [5.0, 20.0, 35.0]) == (10.0, 30.0)


def test_no_exported_start_sits_within_point_three_before_a_cut():
    cuts = [12.2, 40.1, 55.0]
    for start in [11.9, 12.0, 12.15, 39.8, 39.9, 54.75]:
        s, _ = snap_to_shots(start, start + 20, cuts)
        assert not any(0 < c - s < 0.3 for c in cuts), (start, s)


def test_snap_clip_records_original_range_and_moves_cold_open():
    clip = {"start": 10.0, "end": 30.0, "cold_open": {"start": 20.1, "end": 24.0, "text": "x"}}
    snap_clip(clip, [9.8, 19.9], [])
    assert clip["start"] == 9.8
    assert clip["pre_shot_snap"] == {"start": 10.0, "end": 30.0}
    assert clip["cold_open"]["start"] == 19.9
