import os

import pytest
from fastapi import HTTPException

from clippers import jobs_store, server, storage


@pytest.fixture
def home(tmp_path, monkeypatch):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(server.paths, "DOWNLOAD_DIR", str(downloads))
    monkeypatch.setattr(jobs_store, "JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(storage, "STORE_PATH", str(tmp_path / "saved_clips.json"))
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "JOBS", {})
    return downloads


def test_deletes_saved_clips_jobs_and_source(home):
    video = home / "talk.mp4"
    for p in (video, home / "talk.shots.json", home / "talk.diarization.json"):
        p.write_text("x")
    job = {"status": "done", "video_id": "talk", "filepath": str(video), "created_at": 1.0}
    server.JOBS["a"] = dict(job)
    jobs_store.save_job("a", job)
    jobs_store.save_job("b", {**job, "created_at": 2.0})  # on disk only
    storage.save_session("talk", "Talk", [])
    storage.save_session("other", "Other", [])

    server.delete_video("talk")

    assert os.listdir(home) == []
    assert jobs_store.load_all() == {}
    assert server.JOBS == {}
    assert [s["video_id"] for s in storage.load_sessions()] == ["other"]


def test_never_deletes_files_outside_downloads(home, tmp_path):
    outside = tmp_path / "mine.mp4"
    outside.write_text("x")
    server.JOBS["a"] = {"status": "done", "video_id": "mine", "filepath": str(outside)}

    server.delete_video("mine")

    assert outside.exists()
    assert server.JOBS == {}


def test_refuses_while_analyzing(home):
    server.JOBS["a"] = {"status": "transcribing", "video_id": "talk"}
    with pytest.raises(HTTPException) as e:
        server.delete_video("talk")
    assert e.value.status_code == 409
    assert "a" in server.JOBS
