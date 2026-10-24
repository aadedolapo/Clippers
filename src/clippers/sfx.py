"""
Sound-effect selection: index a folder of sound effects (SFX_DIR, e.g. a
DaVinci Resolve Sound Library folder of Epidemic Sound or similar packs)
into whoosh/riser/impact buckets by filename, so a
clip render can grab a soft whoosh for the zoom-out and, optionally, a
riser for the payoff line.
"""

import json
import os
import random
from typing import Optional

from clippers import paths

DATA_DIR = paths.DATA_DIR
INDEX_PATH = os.path.join(DATA_DIR, "sfx_index.json")

AUDIO_EXTS = (".mp3", ".wav", ".aiff", ".m4a", ".aac")

# Filename substrings, checked in order — a file matching "whoosh" before
# "riser" (e.g. "Riser, Transition, Whoosh, Fast") is bucketed as riser
# since that ordering matches how these packs are actually named (the
# leading tag is the primary category in the Epidemic Sound export names).
_CATEGORY_HINTS = [
    ("riser", "riser"),
    ("swoosh", "whoosh"),
    ("whoosh", "whoosh"),
    ("impact", "impact"),
    ("hit", "impact"),
]


def _categorize(filename: str) -> Optional[str]:
    lowered = filename.lower()
    for hint, category in _CATEGORY_HINTS:
        if hint in lowered:
            return category
    return None


def build_index(sfx_dir: str) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {"whoosh": [], "riser": [], "impact": []}
    for root, _dirs, files in os.walk(sfx_dir):
        for name in files:
            if not name.lower().endswith(AUDIO_EXTS):
                continue
            category = _categorize(name)
            if category:
                index[category].append(os.path.join(root, name))

    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_path = INDEX_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
    os.replace(tmp_path, INDEX_PATH)
    return index


def _load_index() -> Optional[dict[str, list[str]]]:
    sfx_dir = os.path.expanduser(os.environ.get("SFX_DIR", "").strip())
    if not sfx_dir or not os.path.isdir(sfx_dir):
        return None
    if not os.path.exists(INDEX_PATH):
        return build_index(sfx_dir)
    with open(INDEX_PATH, encoding="utf-8") as f:
        return json.load(f)


def _pick(category: str) -> Optional[str]:
    index = _load_index()
    if not index:
        return None
    choices = index.get(category, [])
    return random.choice(choices) if choices else None


def pick_whoosh() -> Optional[str]:
    return _pick("whoosh")


def pick_riser() -> Optional[str]:
    return _pick("riser")


if __name__ == "__main__":
    import sys

    sfx_dir = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SFX_DIR", ""))
    idx = build_index(sfx_dir)
    for category, files in idx.items():
        print(f"{category}: {len(files)} files")
