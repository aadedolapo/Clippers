"""
Score-vs-retention eval (improvement plan #7): does the scorer rank the
clips that actually held viewers higher? Run it before and after any change
to scoring.WEIGHTS or the scoring prompt.

    clippers eval export         # refresh eval/clips.json from performance.json
    clippers eval --weights-only  # re-weight stored scorecards (free, instant)
    clippers eval                 # re-score every clip with the current prompt (API calls)
    clippers eval --visual        # ...and the visual rerank (needs the source videos)

Prints the Spearman rank correlation between combined score and stayed-to-
watch %, overall and per lane / duration bucket, and appends the result to
eval/results.jsonl (in the Clippers home folder) so runs can be compared
over time.
"""

import argparse
import json
import os
import subprocess
import time
from typing import Optional

from clippers import __version__, jobs_store, paths, performance

EVAL_DIR = paths.EVAL_DIR
CLIPS_PATH = os.path.join(EVAL_DIR, "clips.json")
RESULTS_PATH = os.path.join(EVAL_DIR, "results.jsonl")
DOWNLOAD_DIR = paths.DOWNLOAD_DIR
# Groups smaller than this get no correlation; it would be noise.
MIN_GROUP = 4


# --- statistics ----------------------------------------------------------------

def _ranks(values: list[float]) -> list[float]:
    """1-based ranks, ties get the average of the ranks they span."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> Optional[float]:
    """Spearman's rho (Pearson correlation of ranks). None if undefined."""
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy) ** 0.5


# --- export --------------------------------------------------------------------

def _video_path(video_id: str) -> Optional[str]:
    if not os.path.isdir(DOWNLOAD_DIR):
        return None
    for name in os.listdir(DOWNLOAD_DIR):
        stem, ext = os.path.splitext(name)
        if stem == video_id and ext.lower() in (".mp4", ".mov", ".mkv", ".webm", ".m4v"):
            return os.path.join(DOWNLOAD_DIR, name)
    return None


def export() -> int:
    """Write every posted clip with a stayed-to-watch % into clips.json,
    with its transcript segments (from the saved job) so re-scoring later
    doesn't depend on the job store still having it."""
    records = [r for r in performance.load_records() if r.get("stayed_to_watch_pct") is not None]
    jobs: dict[str, dict] = {}
    clips = []
    for r in records:
        vid = r["video_id"]
        if vid not in jobs:
            found = jobs_store.find_done_job_by_video_id(vid)
            jobs[vid] = found[1] if found else {}
        segments = [
            s for s in jobs[vid].get("segments", [])
            if s["end"] > r["start"] and s["start"] < r["end"]
        ]
        clips.append({
            "video_id": vid,
            "start": r["start"],
            "end": r["end"],
            "stayed_to_watch_pct": r["stayed_to_watch_pct"],
            "lane": r.get("lane"),
            "duration": r.get("duration") or round(r["end"] - r["start"], 1),
            "hook": r.get("hook"),
            "scorecard": r.get("scorecard"),
            "segments": segments,
        })
    os.makedirs(EVAL_DIR, exist_ok=True)
    with open(CLIPS_PATH, "w", encoding="utf-8") as f:
        json.dump(clips, f, indent=2)
    missing = sum(1 for c in clips if not c["segments"])
    print(f"Wrote {len(clips)} clips to {CLIPS_PATH}"
          + (f" ({missing} without transcript segments; they can only be used with --weights-only)" if missing else ""))
    return len(clips)


# --- scoring -------------------------------------------------------------------

def _rescore(clip: dict, client, energy_cache: dict, use_visual: bool) -> Optional[float]:
    from clippers import llm_scoring, scoring, visual_rerank
    from clippers.audio_energy import analyze_energy, window_energy_score

    if not clip["segments"]:
        return None
    window = {"start": clip["start"], "end": clip["end"], "segments": clip["segments"]}
    lengths = llm_scoring.LONG if clip["end"] - clip["start"] > llm_scoring.SHORT.max_s else llm_scoring.SHORT
    results = llm_scoring.score_batch(client, [window], lengths)
    if not results:
        return None
    scored = results[0]
    scored["start"], scored["end"] = clip["start"], clip["end"]

    audio = 0.5
    path = _video_path(clip["video_id"])
    if path:
        if path not in energy_cache:
            energy_cache[path] = analyze_energy(path)
        audio = window_energy_score(energy_cache[path], clip["start"], clip["end"])

    visual = None
    if use_visual and path:
        visual_rerank.rerank(path, [scored])
        visual = scored["visual"]["score"] if scored.get("visual") else None
    clip["rescored_scorecard"] = scored["scorecard"]
    return scoring.combined_score(scored["scorecard"], audio, visual)


def _stored_score(clip: dict) -> Optional[float]:
    from clippers import scoring

    if not clip.get("scorecard"):
        return None
    # Audio and visual weren't stored with the record; hold them neutral so
    # they don't move the ranking.
    return scoring.combined_score(clip["scorecard"], 0.5, None)


def _git_commit() -> Optional[str]:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=paths.PACKAGE_DIR,
                             capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return None  # installed from a package, not run from a checkout
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=paths.PACKAGE_DIR,
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return out.stdout.strip() + ("+dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return None


def _report(rows: list[tuple[dict, float]], label: str) -> dict:
    def rho(group):
        if len(group) < MIN_GROUP:
            return None
        return spearman([s for _, s in group], [c["stayed_to_watch_pct"] for c, _ in group])

    def fmt(r):
        return "  n/a" if r is None else f"{r:+.3f}"

    overall = rho(rows)
    print(f"\n{label}: {len(rows)} clips")
    print(f"  Spearman(score, stayed %) overall: {fmt(overall)}")

    by_lane, by_bucket = {}, {}
    for c, s in rows:
        by_lane.setdefault(c.get("lane") or "unknown", []).append((c, s))
        by_bucket.setdefault(performance._duration_bucket(c.get("duration")), []).append((c, s))
    lanes = {k: rho(v) for k, v in sorted(by_lane.items())}
    buckets = {k: rho(v) for k, v in sorted(by_bucket.items())}
    print("  By lane:")
    for k, v in sorted(by_lane.items()):
        print(f"    {k:15s} {fmt(lanes[k])}  n={len(v)}")
    print("  By duration:")
    for k, v in sorted(by_bucket.items()):
        print(f"    {k:10s} {fmt(buckets[k])}  n={len(v)}")
    if len(rows) < MIN_GROUP:
        print(f"  (need at least {MIN_GROUP} clips with stayed % for a correlation)")
    return {"n": len(rows), "spearman": overall, "by_lane": lanes, "by_duration": buckets}


def run(weights_only: bool, use_visual: bool, note: Optional[str]) -> dict:
    from clippers import scoring

    if not os.path.exists(CLIPS_PATH):
        print(f"{CLIPS_PATH} not found; exporting from performance.json first.")
        export()
    with open(CLIPS_PATH, encoding="utf-8") as f:
        clips = json.load(f)

    rows = []
    if weights_only:
        mode = "weights-only"
        for c in clips:
            s = _stored_score(c)
            if s is not None:
                rows.append((c, s))
    else:
        from anthropic import Anthropic

        mode = "rescore+visual" if use_visual else "rescore"
        client = Anthropic()
        energy_cache: dict = {}
        for i, c in enumerate(clips, 1):
            print(f"  [{i}/{len(clips)}] {c['video_id']} {c['start']:.1f}-{c['end']:.1f}", flush=True)
            try:
                s = _rescore(c, client, energy_cache, use_visual)
            except Exception as e:
                print(f"    failed: {e}")
                s = None
            if s is not None:
                rows.append((c, s))

    result = _report(rows, mode)
    entry = {"t": time.time(), "mode": mode, "version": __version__, "commit": _git_commit(), "weights": scoring.WEIGHTS,
             "note": note, **result}
    with open(RESULTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"\nLogged to {RESULTS_PATH}")
    return entry


def main(argv: Optional[list] = None):
    parser = argparse.ArgumentParser(description="Score vs. stayed-to-watch % eval")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "export"])
    parser.add_argument("--weights-only", action="store_true",
                        help="re-weight each clip's stored scorecard instead of calling the API")
    parser.add_argument("--visual", action="store_true", help="include the visual rerank when re-scoring")
    parser.add_argument("--note", help="free-text note saved with the result (e.g. what you changed)")
    args = parser.parse_args(argv)
    if args.command == "export":
        export()
    else:
        run(args.weights_only, args.visual, args.note)


if __name__ == "__main__":
    main()
