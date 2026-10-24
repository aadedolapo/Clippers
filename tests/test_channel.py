from clippers import channel, llm_scoring


def test_slug_matches_existing_lane_ids():
    assert channel._slug("men's growth") == "mens_growth"
    assert channel._slug(" Faith ") == "faith"


def test_no_lanes_scores_on_broad_appeal(monkeypatch):
    monkeypatch.setattr(channel, "LANES", [])
    monkeypatch.setattr(channel, "LANE_LABELS", [])
    prompt = llm_scoring.system_prompt()
    assert "no fixed topics" in prompt
    assert "snake_case topic label" in prompt


def test_lanes_go_into_the_prompt(monkeypatch):
    monkeypatch.setattr(channel, "LANE_LABELS", ["faith", "men's growth"])
    monkeypatch.setattr(channel, "LANES", ["faith", "mens_growth"])
    monkeypatch.setattr(channel, "DESCRIPTION", "a faith channel")
    prompt = llm_scoring.system_prompt()
    assert '"lane": one of "faith", "mens_growth", "other"' in prompt
    assert "core lanes are faith, men's growth" in prompt
    assert "The clips are for a faith channel." in prompt
    for placeholder in ("{channel}", "{lane_field}", "{lane_rule}"):
        assert placeholder not in prompt
