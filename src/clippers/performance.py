"""
Performance log and feedback loop (retention upgrade plan phase 5): a
durable record of how each posted clip actually did, so llm_scoring.py can
eventually learn what THIS channel's audience holds for instead of relying
only on generic retention heuristics. File-backed, matching storage.py's
pattern — no database, single user, localhost.
"""

import csv
import io
import json
import os
import threading
import time
from typing import Optional

from clippers import paths

DATA_DIR = paths.DATA_DIR
STORE_PATH = os.path.join(DATA_DIR, "performance.json")

_LOCK = threading.Lock()

# llm_scoring.py's "what works on this channel" feedback block only kicks in
# once there's enough real data to be a signal rather than noise.
MIN_RECORDS_FOR_FEEDBACK = 10


def clip_key(video_id: str, start: float, end: float) -> str:
    return f"{video_id}:{start:.2f}-{end:.2f}"


def add_or_update_record(
    video_id: str,
    start: float,
    end: float,
    youtube_url: Optional[str] = None,
    posted_at: Optional[float] = None,
    stayed_to_watch_pct: Optional[float] = None,
    views_48h: Optional[int] = None,
    views_7d: Optional[int] = None,
    likes: Optional[int] = None,
    subs_gained: Optional[int] = None,
    clip_snapshot: Optional[dict] = None,
) -> dict:
    """Create the record on first call (typically just marking a clip as
    posted, with a youtube_url) or merge in whichever fields are given on a
    later call (manual stats entry, or a CSV import row) — a field already
    on the record is only overwritten when the caller actually supplies a
    new value, so partial updates compose instead of clobbering.

    clip_snapshot, when given, is the clip's own serialized dict (as
    main.py's _serialize_clip produces) — its hook/duration/scorecard/
    cold_open are copied in so the record still means something even if the
    original session's storage entry is later pruned or changes."""
    key = clip_key(video_id, start, end)
    with _LOCK:
        data = _read()
        record = data.get(key) or {
            "clip_key": key,
            "video_id": video_id,
            "start": start,
            "end": end,
            "posted_at": None,
            "youtube_url": None,
            "stayed_to_watch_pct": None,
            "views_48h": None,
            "views_7d": None,
            "likes": None,
            "subs_gained": None,
            "hook": None,
            "lane": None,
            "duration": None,
            "cold_open_used": None,
            "scorecard": None,
        }

        if clip_snapshot is not None:
            record["hook"] = clip_snapshot.get("hook") or record.get("hook")
            record["duration"] = clip_snapshot.get("duration", record.get("duration"))
            sc = clip_snapshot.get("scorecard") or None
            if sc:
                record["scorecard"] = sc
                record["lane"] = sc.get("lane", record.get("lane"))
            record["cold_open_used"] = bool(clip_snapshot.get("cold_open"))

        for field, value in [
            ("youtube_url", youtube_url),
            ("posted_at", posted_at),
            ("stayed_to_watch_pct", stayed_to_watch_pct),
            ("views_48h", views_48h),
            ("views_7d", views_7d),
            ("likes", likes),
            ("subs_gained", subs_gained),
        ]:
            if value is not None:
                record[field] = value
        if record["posted_at"] is None:
            record["posted_at"] = time.time()

        data[key] = record
        _write(data)
        return record


def load_records() -> list[dict]:
    with _LOCK:
        data = _read()
    return list(data.values())


def _read() -> dict:
    if not os.path.exists(STORE_PATH):
        return {}
    with open(STORE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _write(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_path = STORE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, STORE_PATH)


# --- CSV import -------------------------------------------------------------

# YouTube Studio's Shorts export (Content -> Shorts -> Export) column names
# vary by export type/locale — matched by substring against the header
# rather than an exact name so this survives minor header drift.
_COLUMN_HINTS = {
    "title": ["video title", "content title", "title"],
    "url": ["video url", "content url", "url", "link"],
    "stayed_to_watch_pct": ["average percentage viewed", "audience retention", "percentage viewed"],
    "views": ["views"],
    "likes": ["likes"],
    "subs_gained": ["subscribers"],
}


def _find_column(headers: list[str], hints: list[str]) -> Optional[str]:
    for hint in hints:
        for h in headers:
            if hint in h.lower():
                return h
    return None


def _parse_number(raw: str) -> Optional[float]:
    try:
        return float(raw.replace("%", "").replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def import_csv(csv_text: str) -> dict:
    """Match a YouTube Studio export's rows to EXISTING performance records
    by youtube_url (preferred) or, when a row has no URL column, by title
    against the record's own "hook" field — which only works if the clip
    was posted to YouTube using its hook as the video title, a documented
    assumption rather than a guarantee. Never creates a new record: a clip
    has to already be marked posted (via add_or_update_record) for its
    clip_key/hook/scorecard snapshot to exist to update. Returns
    {"matched": n, "unmatched": n} row counts."""
    reader = csv.DictReader(io.StringIO(csv_text))
    headers = reader.fieldnames or []
    title_col = _find_column(headers, _COLUMN_HINTS["title"])
    url_col = _find_column(headers, _COLUMN_HINTS["url"])
    stayed_col = _find_column(headers, _COLUMN_HINTS["stayed_to_watch_pct"])
    views_col = _find_column(headers, _COLUMN_HINTS["views"])
    likes_col = _find_column(headers, _COLUMN_HINTS["likes"])
    subs_col = _find_column(headers, _COLUMN_HINTS["subs_gained"])

    matched, unmatched = 0, 0
    with _LOCK:
        data = _read()
        by_url = {r["youtube_url"]: k for k, r in data.items() if r.get("youtube_url")}

        for row in reader:
            key = None
            if url_col and row.get(url_col) in by_url:
                key = by_url[row[url_col]]
            elif title_col:
                wanted = (row.get(title_col) or "").strip().lower()
                key = next(
                    (k for k, r in data.items() if r.get("hook") and r["hook"].strip().lower() == wanted),
                    None,
                )
            if key is None:
                unmatched += 1
                continue

            record = data[key]
            if stayed_col and row.get(stayed_col):
                val = _parse_number(row[stayed_col])
                if val is not None:
                    record["stayed_to_watch_pct"] = val
            if views_col and row.get(views_col):
                val = _parse_number(row[views_col])
                if val is not None:
                    # A weekly Studio export's aggregate view count maps to
                    # views_7d; views_48h is only ever set by hand (a quick
                    # early check, well before a weekly export would exist).
                    record["views_7d"] = int(val)
            if likes_col and row.get(likes_col):
                val = _parse_number(row[likes_col])
                if val is not None:
                    record["likes"] = int(val)
            if subs_col and row.get(subs_col):
                val = _parse_number(row[subs_col])
                if val is not None:
                    record["subs_gained"] = int(val)
            matched += 1

        _write(data)

    return {"matched": matched, "unmatched": unmatched}


# --- reporting ---------------------------------------------------------------

def _duration_bucket(duration: Optional[float]) -> str:
    if duration is None:
        return "unknown"
    if duration < 20:
        return "<20s"
    if duration < 30:
        return "20-30s"
    if duration <= 45:
        return "30-45s"
    return ">45s"


def print_report() -> None:
    scored = [r for r in load_records() if r.get("stayed_to_watch_pct") is not None]
    if not scored:
        print("No performance records with stayed_to_watch_pct yet.")
        return

    def avg(vals: list[float]) -> float:
        return sum(vals) / len(vals)

    print(f"{len(scored)} records with stayed_to_watch_pct data\n")

    print("By lane:")
    for lane in sorted({r.get("lane") or "unknown" for r in scored}):
        vals = [r["stayed_to_watch_pct"] for r in scored if (r.get("lane") or "unknown") == lane]
        print(f"  {lane:15s} avg={avg(vals):5.1f}%  n={len(vals)}")

    print("\nBy duration bucket:")
    for bucket in sorted({_duration_bucket(r.get("duration")) for r in scored}):
        vals = [r["stayed_to_watch_pct"] for r in scored if _duration_bucket(r.get("duration")) == bucket]
        print(f"  {bucket:10s} avg={avg(vals):5.1f}%  n={len(vals)}")

    print("\nCold open vs none:")
    for used, label in ((True, "cold open"), (False, "no cold open")):
        vals = [r["stayed_to_watch_pct"] for r in scored if bool(r.get("cold_open_used")) == used]
        if vals:
            print(f"  {label:14s} avg={avg(vals):5.1f}%  n={len(vals)}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "report":
        print_report()
    else:
        print("Usage: clippers report")
