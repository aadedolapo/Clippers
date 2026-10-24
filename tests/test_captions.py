import re

from clippers import captions
from conftest import seg


def _dialogues(ass):
    return [line for line in ass.splitlines() if line.startswith("Dialogue:")]


def _times(line):
    _, start, end = line.split(",")[:3]
    def to_s(t):
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    return to_s(start), to_s(end)


def test_fmt_ass_time():
    assert captions._fmt_ass_time(0) == "0:00:00.00"
    assert captions._fmt_ass_time(3725.5) == "1:02:05.50"
    assert captions._fmt_ass_time(-1) == "0:00:00.00"


def test_generate_ass_times_are_clip_relative():
    segments = [seg(100.0, 103.0, "one two three"), seg(103.0, 106.0, "four five six seven eight nine")]
    ass = captions.generate_ass(segments, 100.0, 106.0, [])
    lines = _dialogues(ass)
    assert lines
    first_start, _ = _times(lines[0])
    assert first_start == captions.FIRST_CAPTION_DELAY
    for line in lines:
        a, b = _times(line)
        assert 0 <= a <= b <= 6.0


def test_generate_ass_excludes_words_outside_clip():
    segments = [seg(0, 3, "before before before"), seg(10, 13, "inside inside inside")]
    ass = captions.generate_ass(segments, 9.0, 14.0, [])
    assert "before" not in ass and "inside" in ass


def test_karaoke_durations_cover_each_word():
    segments = [seg(0, 3, "a b c")]
    ass = captions.generate_ass(segments, 0, 3, [])
    ks = [int(k) for k in re.findall(r"\\k(\d+)", ass)]
    assert len(ks) == 3 and all(k > 0 for k in ks)


def test_emphasis_word_is_scaled():
    ass = captions.generate_ass([seg(0, 3, "spend $240 monthly")], 0, 3, ["$240"])
    assert f"\\fscx{captions.EMPHASIS_SCALE}" in ass


def test_play_res_follows_aspect():
    assert "PlayResY: 1350" in captions.generate_ass([seg(0, 1, "a b")], 0, 1, [], out_h=1350)
    mf = captions.generate_minimal_faith_ass([seg(0, 2, "hello there friend")], 0, 2, [], out_h=1080)
    assert "PlayResY: 1080" in mf
    assert "\\pos(540,810)" in mf or "\\move(" in mf


def test_minimal_faith_phrases_never_overlap():
    segments = [seg(0, 6, "God will not leave you where you started today friend")]
    lines = _dialogues(captions.generate_minimal_faith_ass(segments, 0, 6, ["started"]))
    spans = [_times(line) for line in lines]
    for (a1, b1), (a2, _) in zip(spans, spans[1:]):
        assert b1 <= a2 + 1e-6


def test_clip_tag_default_aspect_unchanged():
    assert captions.clip_tag("/x/vid.mp4", 1, 2, "p") == "vid_1.0-2.0_p"
    assert captions.clip_tag("/x/vid.mp4", 1, 2, "p", "1:1") == "vid_1.0-2.0_p_1x1"
