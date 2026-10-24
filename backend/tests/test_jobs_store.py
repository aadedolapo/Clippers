import pytest

import jobs_store


@pytest.fixture(autouse=True)
def temp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_DIR", str(tmp_path))


def test_round_trip_and_numpy_values():
    import numpy as np

    jobs_store.save_job("a", {"status": "done", "video_id": "v", "created_at": 1.0,
                              "clips": [{"combined_score": np.float32(0.5)}]})
    job = jobs_store.load_job("a")
    assert job["clips"][0]["combined_score"] == 0.5


def test_running_jobs_come_back_interrupted():
    jobs_store.save_job("run", {"status": "transcribing", "created_at": 1.0})
    jobs_store.save_job("ok", {"status": "done", "video_id": "v", "created_at": 2.0})
    jobs = jobs_store.load_all()
    assert jobs["run"]["status"] == "error" and jobs["run"]["interrupted"]
    assert jobs["ok"]["status"] == "done"
    # The interrupted state is written back, so it stays that way.
    assert jobs_store.load_job("run")["status"] == "error"


def test_find_done_job_by_video_id_picks_newest():
    jobs_store.save_job("old", {"status": "done", "video_id": "v", "created_at": 1.0})
    jobs_store.save_job("new", {"status": "done", "video_id": "v", "created_at": 5.0})
    jobs_store.save_job("err", {"status": "error", "video_id": "v", "created_at": 9.0})
    assert jobs_store.find_done_job_by_video_id("v")[0] == "new"
    assert jobs_store.find_done_job_by_video_id("other") is None
