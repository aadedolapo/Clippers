import pytest

from clippers import resolve_integration, server


@pytest.fixture
def job(monkeypatch):
    job = {
        "title": "Test", "filepath": "/videos/test.mp4", "segments": [], "speaker_turns": [],
        "clips": [
            {"start": 10.0, "end": 30.0, "emphasis_words": []},
            {"start": 40.0, "end": 60.0, "emphasis_words": []},
        ],
    }
    monkeypatch.setattr(server, "_find_job_by_video_id", lambda vid: job)
    monkeypatch.setattr(server.captions, "render_minimal_faith_clip",
                        lambda **kw: f"/out/{kw['start']:.0f}.mp4")
    return job


def _send(clips):
    req = server.SendToResolveRequest(video_id="test", clips=[server.ClipSelection(start=a, end=b) for a, b in clips])
    return server.send_to_resolve(req)["results"]


def test_renders_mp4_when_resolve_unreachable(job, monkeypatch):
    attempts = []

    def unreachable():
        attempts.append(1)
        raise resolve_integration.ResolveUnavailable("Could not connect to DaVinci Resolve.")

    monkeypatch.setattr(resolve_integration, "_get_project", unreachable)
    results = _send([(10.0, 30.0), (40.0, 60.0)])
    assert [r["success"] for r in results] == [True, True]
    assert [r["captioned_mp4"] for r in results] == ["/out/10.mp4", "/out/40.mp4"]
    assert all(r["in_resolve"] is False and "Could not connect" in r["resolve_error"] for r in results)
    # The rest of the request skips Resolve instead of reconnecting per clip.
    assert len(attempts) == 1


def test_one_clip_failing_in_resolve_still_renders(job, monkeypatch):
    def boom(**kw):
        raise RuntimeError("AppendToTimeline failed")

    monkeypatch.setattr(resolve_integration, "send_clip_to_resolve", boom)
    (r,) = _send([(10.0, 30.0)])
    assert r["success"] and r["captioned_mp4"] == "/out/10.mp4"
    assert r["in_resolve"] is False and r["resolve_error"] == "AppendToTimeline failed"


def test_sent_to_resolve(job, monkeypatch):
    monkeypatch.setattr(resolve_integration, "send_clip_to_resolve", lambda **kw: None)
    (r,) = _send([(10.0, 30.0)])
    assert r["success"] and r["in_resolve"] is True and r["resolve_error"] is None
