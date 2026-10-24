import pytest
from fastapi.testclient import TestClient

from clippers import llm_scoring, server
from conftest import seg


def test_analyze_refuses_without_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    res = TestClient(server.app).post("/analyze", json={"url": "https://example.com/v"})
    assert res.status_code == 400
    assert "ANTHROPIC_API_KEY" in res.json()["detail"]


def test_scoring_raises_when_every_batch_fails(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    def fail(*args, **kwargs):
        raise RuntimeError("bad key")

    monkeypatch.setattr(llm_scoring, "score_batch", fail)
    segments = [seg(i * 5.0, i * 5.0 + 5.0, f"line {i}") for i in range(20)]
    with pytest.raises(RuntimeError, match="every batch"):
        llm_scoring.score_all_windows(segments)
