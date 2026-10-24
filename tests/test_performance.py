import pytest

from clippers import performance


@pytest.fixture(autouse=True)
def temp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(performance, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(performance, "STORE_PATH", str(tmp_path / "performance.json"))


def test_find_column_matches_by_substring_and_hint_order():
    headers = ["Content", "Video title", "Average percentage viewed (%)", "Views"]
    assert performance._find_column(headers, performance._COLUMN_HINTS["title"]) == "Video title"
    assert performance._find_column(headers, performance._COLUMN_HINTS["stayed_to_watch_pct"]) == "Average percentage viewed (%)"
    assert performance._find_column(headers, ["missing"]) is None


def test_import_csv_matches_by_url_then_title():
    performance.add_or_update_record("v1", 0, 30, youtube_url="https://youtu.be/a")
    performance.add_or_update_record("v2", 0, 30, clip_snapshot={"hook": "One sentence changed everything"})
    csv_text = (
        "Video title,Video URL,Average percentage viewed (%),Views,Likes\n"
        "whatever,https://youtu.be/a,71.5%,\"1,200\",30\n"
        "one sentence changed everything,,55,900,12\n"
        "Unknown clip,,40,10,1\n"
    )
    assert performance.import_csv(csv_text) == {"matched": 2, "unmatched": 1}
    recs = {r["video_id"]: r for r in performance.load_records()}
    assert recs["v1"]["stayed_to_watch_pct"] == 71.5
    assert recs["v1"]["views_7d"] == 1200
    assert recs["v2"]["stayed_to_watch_pct"] == 55
    assert recs["v2"]["likes"] == 12


def test_import_csv_never_creates_records():
    performance.import_csv("Video title,Views\nNew one,5\n")
    assert performance.load_records() == []
