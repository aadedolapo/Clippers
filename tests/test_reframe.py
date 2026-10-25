import shutil
import subprocess

import pytest

from clippers import reframe


def test_crop_size_portrait_from_landscape():
    assert reframe.crop_size(1920, 1080, 9, 16) == (606, 1080)
    assert reframe.crop_size(1920, 1080, 1, 1) == (1080, 1080)
    # Portrait source into a wider target crops height instead.
    w, h = reframe.crop_size(1080, 1920, 4, 5)
    assert w == 1080 and h < 1920


def test_clamp_window_stays_inside_frame():
    assert reframe.clamp_window(10, 540, 1920, 1080, 606, 1080) == (0.0, 0.0)
    assert reframe.clamp_window(1910, 540, 1920, 1080, 606, 1080) == (1920 - 606, 0.0)
    assert reframe.clamp_window(960, 540, 1920, 1080, 606, 1080) == (960 - 303, 0.0)


def test_smooth_path_ignores_moves_inside_dead_zone():
    targets = [(960, 540), (980, 540), (950, 540), (970, 540)]
    path = reframe.smooth_path(targets, crop_w=606)
    assert all(abs(x - 960) < 1e-6 for x, _ in path)


def test_smooth_path_hard_cuts_on_persistent_big_jump():
    targets = [(400, 540)] * 5 + [(1500, 540)] * 6
    path = reframe.smooth_path(targets, crop_w=606, window=1)
    assert path[4][0] == 400
    # One-sample blip doesn't move it, the second sample of the new position cuts.
    assert path[5][0] == 400
    assert path[6][0] == 1500


def test_smooth_path_single_blip_is_ignored():
    targets = [(400, 540)] * 5 + [(1500, 540)] + [(400, 540)] * 5
    path = reframe.smooth_path(targets, crop_w=606)
    assert max(x for x, _ in path) < 401


def test_smooth_path_fills_gaps():
    path = reframe.smooth_path([None, (500, 540), None, None], crop_w=606)
    assert len(path) == 4 and all(abs(x - 500) < 1e-6 for x, _ in path)
    assert reframe.smooth_path([None, None], crop_w=606) == []


def face(cx, w, track=None, mouth=0.0):
    f = {"cx": cx, "cy": 540, "w": w, "h": w, "score": 0.9, "mouth": mouth}
    if track is not None:
        f["track"] = track
    return f


def test_sticky_largest_does_not_ping_pong_between_similar_faces():
    samples = [{"t": i / 5, "faces": [face(400, 100 + (i % 2) * 8), face(1500, 104)]} for i in range(10)]
    reframe.assign_tracks(samples, 1920)
    chosen = reframe.choose_targets(samples)
    assert len({c["track"] for c in chosen}) == 1


def test_speaker_turns_follow_the_moving_mouth():
    samples = []
    for i in range(20):
        t = i / 5
        left_talking = t < 2.0
        samples.append({"t": t, "faces": [face(400, 120, mouth=5.0 if left_talking else 0.1),
                                           face(1500, 100, mouth=0.1 if left_talking else 5.0)]})
    reframe.assign_tracks(samples, 1920)
    turns = [{"start": 0.0, "end": 2.0, "speaker": "A"}, {"start": 2.0, "end": 4.0, "speaker": "B"}]
    chosen = reframe.choose_targets(samples, turns)
    assert chosen[2]["cx"] == 400
    assert chosen[15]["cx"] == 1500


def test_write_sendcmd_one_line_per_frame(tmp_path):
    path = {"times": [0.0, 1.0], "xy": [(0.0, 0.0), (100.0, 0.0)], "fps": 10.0}
    out = reframe.write_sendcmd(path, 1.0, str(tmp_path / "c.cmd"))
    lines = open(out).read().splitlines()
    assert len(lines) == 10
    assert lines[5].startswith("0.5000 crop@rf x 50.0")


def test_center_crop_filter_mentions_aspect():
    assert "ih*9/16" in reframe.center_crop_filter((9, 16))


def test_filter_path_windows():
    # Backslashes become forward slashes; the drive colon is escaped for both levels.
    assert reframe.filter_path(r"C:\Users\me\a.ass") == r"C\\:/Users/me/a.ass"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_filter_path_survives_ffmpeg(tmp_path):
    # A folder name with every character that's special in a filter graph.
    d = tmp_path / "it's, [odd]; name"
    d.mkdir()
    ass = d / "t.ass"
    ass.write_text("[Script Info]\nPlayResX: 64\nPlayResY: 64\n"
                   "[V4+ Styles]\nFormat: Name, Fontname, Fontsize\nStyle: Default,Arial,20\n[Events]\n"
                   "Format: Layer, Start, End, Style, Text\n"
                   "Dialogue: 0,0:00:00.00,0:00:01.00,Default,HI\n", encoding="utf-8")
    vf = f"ass={reframe.filter_path(str(ass))}:fontsdir={reframe.filter_path(str(d))}"
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=black:s=64x64:d=0.2",
                        "-vf", vf, str(tmp_path / "o.mp4")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
