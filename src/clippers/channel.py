"""
What the channel is about, from .env, so scoring and commentary can be
tuned to one channel without editing the prompts:

    CHANNEL_DESCRIPTION=a faith and self-improvement channel for young men
    CHANNEL_LANES=faith, men's growth, discipline, purpose

Lanes are the channel's core topics. The scorer rates how well each clip
fits them (lane_fit) and tags it with one; Today's picks takes at most one
clip per lane. With no lanes set, clips are rated on broad appeal and
tagged with a free-form topic instead.
"""

import os
import re


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower().replace("'", "")).strip("_")


DESCRIPTION = os.environ.get("CHANNEL_DESCRIPTION", "").strip()
LANE_LABELS = [lane.strip() for lane in os.environ.get("CHANNEL_LANES", "").split(",") if lane.strip()]
LANES = [_slug(label) for label in LANE_LABELS]


def describe(default: str = "a short-form video channel") -> str:
    return DESCRIPTION or default


def lane_field() -> str:
    """The scorecard's "lane" field, for the scoring prompt's schema."""
    if not LANES:
        return '"lane": a one- or two-word snake_case topic label for the clip (e.g. "money", "parenting")'
    options = ", ".join(f'"{lane}"' for lane in LANES + ["other"])
    return f'"lane": one of {options}'


def lane_rule() -> str:
    """How to score lane_fit, for the scoring prompt's rules."""
    if not LANES:
        return ("Lane: the channel has no fixed topics, so score lane_fit on how broadly the clip's "
                "topic appeals to a general short-form audience.")
    return (f"Lane: the channel's core lanes are {', '.join(LANE_LABELS)}. Topics outside these "
            "score low on lane_fit even if the clip is otherwise strong.")
