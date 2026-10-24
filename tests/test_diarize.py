from clippers import diarize
from conftest import seg

TURNS = [{"start": 0.0, "end": 5.0, "speaker": "A"}, {"start": 5.0, "end": 10.0, "speaker": "B"}]


def test_assign_speakers_labels_words_and_segments():
    segments = [seg(0.5, 4.5, "a b c d"), seg(4.0, 9.0, "e f g h i j k l m n")]
    diarize.assign_speakers(segments, TURNS)
    assert segments[0]["speaker"] == "A"
    assert segments[1]["speaker"] == "B"
    assert segments[1]["words"][0]["speaker"] == "A"


def test_assign_speakers_noop_without_turns():
    segments = [seg(0, 1)]
    diarize.assign_speakers(segments, [])
    assert "speaker" not in segments[0]


def test_turns_in_range_is_clip_relative():
    out = diarize.turns_in_range(TURNS, 3.0, 8.0)
    assert out == [{"start": 0.0, "end": 2.0, "speaker": "A"}, {"start": 2.0, "end": 5.0, "speaker": "B"}]


def test_turns_in_range_single_speaker_is_none():
    assert diarize.turns_in_range(TURNS, 0.0, 4.0) is None
    assert diarize.turns_in_range(None, 0.0, 4.0) is None


def test_label():
    assert diarize.label({"speaker": "SPEAKER_01"}) == " (SPEAKER_01)"
    assert diarize.label({}) == ""
