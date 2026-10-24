"""
Speaker-following crop for the ffmpeg render (improvement plan #1). The
rendered mp4 used a fixed center crop, so an off-center or walking speaker
got cut out of frame. This samples the clip at a few fps, finds faces, picks
the one to follow, smooths that into a camera path, and hands ffmpeg a
per-frame crop via a sendcmd file.

Face detection is OpenCV's YuNet (a ~230KB ONNX model, downloaded on first
use) rather than MediaPipe: MediaPipe 1.x dropped the bundled face-detection
API and needs a model download anyway, and its short-range model misses the
small faces in wide stage shots that sermon footage is full of.

Which face to follow:
  - No diarization: the largest face, with stickiness so two similar-sized
    faces don't ping-pong the frame.
  - With diarization (speaker turns from diarize.py): for each turn, the
    face whose mouth moved most during it, i.e. the one actually talking.
Big target jumps (a switch to the other speaker) become a hard cut instead
of a whip pan; small head moves inside a dead zone don't move the frame.
"""

import json
import logging
import os
import subprocess
import urllib.request
from typing import Optional

import numpy as np

logger = logging.getLogger("viral_clipper.reframe")

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "models")
MODEL_PATH = os.path.join(MODEL_DIR, "face_detection_yunet_2023mar.onnx")
MODEL_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"

SAMPLE_FPS = 5.0
DETECT_WIDTH = 640
SCORE_THRESHOLD = 0.6
# Below this share of sampled frames with a face, the "track" is more
# likely noise than a speaker; fall back to the center crop.
MIN_FACE_COVERAGE = 0.2

# Camera-path tuning, as fractions of the crop window's width.
DEAD_ZONE = 0.08
JUMP = 0.6
EMA_ALPHA = 0.3
# Samples a big jump has to persist before the camera cuts to it (0.4s at
# 5fps): a one-frame misdetection shouldn't move anything.
JUMP_HOLD = 2
# Centered moving-average window (samples) applied after the EMA, to take
# the remaining stair-step out of a 5fps path.
SMOOTH_WINDOW = 5

# Faces within this share of the source width between samples are treated
# as the same person.
TRACK_MATCH = 0.12
# A new largest face must be this much bigger to steal focus.
STICKY_RATIO = 1.25

_detector = None


def _get_detector():
    global _detector
    if _detector is None:
        import cv2

        if not os.path.exists(MODEL_PATH):
            os.makedirs(MODEL_DIR, exist_ok=True)
            logger.info(f"downloading YuNet face model to {MODEL_PATH}")
            urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
        _detector = cv2.FaceDetectorYN.create(MODEL_PATH, "", (320, 320), SCORE_THRESHOLD)
    return _detector


def probe_video(filepath: str) -> tuple[int, int, float]:
    """(width, height, fps) of the first video stream."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate", "-of", "json", filepath],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    stream = json.loads(out.stdout)["streams"][0]
    num, den = stream["r_frame_rate"].split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    return int(stream["width"]), int(stream["height"]), fps or 30.0


def crop_size(src_w: int, src_h: int, aspect_w: int, aspect_h: int) -> tuple[int, int]:
    """Largest even-sized window of the target aspect that fits the source."""
    target = aspect_w / aspect_h
    if src_w / src_h > target:
        h = src_h
        w = h * target
    else:
        w = src_w
        h = w / target
    return int(w) // 2 * 2, int(h) // 2 * 2


def clamp_window(cx: float, cy: float, src_w: int, src_h: int, crop_w: int, crop_h: int) -> tuple[float, float]:
    """Top-left corner of a crop_w x crop_h window centered on (cx, cy),
    pushed back inside the frame."""
    x = min(max(cx - crop_w / 2, 0.0), src_w - crop_w)
    y = min(max(cy - crop_h / 2, 0.0), src_h - crop_h)
    return x, y


# --- detection ---------------------------------------------------------------

def _iter_frames(filepath: str, start: float, end: float, src_w: int, src_h: int):
    """Yield (t, bgr_frame) at SAMPLE_FPS, t relative to `start`, frames
    downscaled to DETECT_WIDTH wide."""
    det_w = min(DETECT_WIDTH, src_w) // 2 * 2
    det_h = int(round(src_h * det_w / src_w)) // 2 * 2
    proc = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-ss", str(start), "-t", str(end - start), "-i", filepath,
         "-vf", f"fps={SAMPLE_FPS},scale={det_w}:{det_h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    frame_bytes = det_w * det_h * 3
    i = 0
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield i / SAMPLE_FPS, np.frombuffer(buf, np.uint8).reshape(det_h, det_w, 3)
            i += 1
    finally:
        proc.stdout.close()
        proc.wait()


def _region_diff(gray: np.ndarray, prev: np.ndarray, x0: float, y0: float, x1: float, y1: float) -> float:
    h, w = gray.shape
    xa, xb = max(0, int(x0)), min(w, int(x1))
    ya, yb = max(0, int(y0)), min(h, int(y1))
    if xb - xa < 2 or yb - ya < 2:
        return 0.0
    return float(np.abs(gray[ya:yb, xa:xb].astype(np.int16) - prev[ya:yb, xa:xb].astype(np.int16)).mean())


def detect_faces(filepath: str, start: float, end: float) -> dict:
    """Sample [start, end) and detect faces. Returns
    {"src_w", "src_h", "fps", "samples": [{"t", "faces": [face, ...]}]}
    where each face is {"cx", "cy", "w", "h", "score", "mouth"} in source
    pixels. "mouth" is mouth-region motion since the previous sample minus
    eye-region motion (so head movement mostly cancels out), used to tell
    which face is talking."""
    import cv2

    src_w, src_h, fps = probe_video(filepath)
    detector = _get_detector()
    samples = []
    prev_gray = None
    for t, frame in _iter_frames(filepath, start, end, src_w, src_h):
        det_h, det_w = frame.shape[:2]
        scale = src_w / det_w
        detector.setInputSize((det_w, det_h))
        _, rows = detector.detect(frame)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = []
        for r in rows if rows is not None else []:
            x, y, w, h = (float(v) for v in r[:4])
            mouth = 0.0
            if prev_gray is not None:
                rm_x, rm_y, lm_x, lm_y = (float(v) for v in r[10:14])
                eye_y = (float(r[5]) + float(r[7])) / 2
                m_y = (rm_y + lm_y) / 2
                pad = 0.15 * w
                mouth_motion = _region_diff(gray, prev_gray, rm_x - pad, m_y - 0.12 * h, lm_x + pad, m_y + 0.15 * h)
                eye_motion = _region_diff(gray, prev_gray, x, eye_y - 0.1 * h, x + w, eye_y + 0.1 * h)
                mouth = max(0.0, mouth_motion - eye_motion)
            faces.append({
                "cx": (x + w / 2) * scale, "cy": (y + h / 2) * scale,
                "w": w * scale, "h": h * scale, "score": float(r[14]), "mouth": mouth,
            })
        samples.append({"t": t, "faces": faces})
        prev_gray = gray
    return {"src_w": src_w, "src_h": src_h, "fps": fps, "samples": samples}


# --- choosing the face to follow --------------------------------------------

def assign_tracks(samples: list[dict], src_w: int) -> int:
    """Label each face with a "track" id by nearest horizontal position to
    the previous sample's faces. Returns the number of tracks."""
    last_cx: dict[int, float] = {}
    next_id = 0
    for s in samples:
        taken = set()
        for f in sorted(s["faces"], key=lambda f: f["w"] * f["h"], reverse=True):
            best, best_d = None, TRACK_MATCH * src_w
            for tid, cx in last_cx.items():
                d = abs(f["cx"] - cx)
                if tid not in taken and d < best_d:
                    best, best_d = tid, d
            if best is None:
                best, next_id = next_id, next_id + 1
            f["track"] = best
            taken.add(best)
            last_cx[best] = f["cx"]
    return next_id


def _sticky_largest(samples: list[dict]) -> list[Optional[dict]]:
    out = []
    current = None
    for s in samples:
        if not s["faces"]:
            out.append(None)
            continue
        largest = max(s["faces"], key=lambda f: f["w"] * f["h"])
        held = next((f for f in s["faces"] if f["track"] == current), None)
        if held is not None and held["w"] * held["h"] * STICKY_RATIO >= largest["w"] * largest["h"]:
            out.append(held)
        else:
            current = largest["track"]
            out.append(largest)
    return out


def choose_targets(samples: list[dict], speaker_turns: Optional[list[dict]] = None) -> list[Optional[dict]]:
    """The face to follow at each sample (None where there isn't one).
    speaker_turns are {"start", "end", "speaker"} relative to the clip
    start. Tracks must already be assigned (assign_tracks)."""
    fallback = _sticky_largest(samples)
    if not speaker_turns:
        return fallback

    tracks_seen: dict[int, int] = {}
    for s in samples:
        for f in s["faces"]:
            tracks_seen[f["track"]] = tracks_seen.get(f["track"], 0) + 1
    real_tracks = [t for t, n in tracks_seen.items() if n >= MIN_FACE_COVERAGE * len(samples)]
    if len(real_tracks) < 2:
        return fallback

    out = list(fallback)
    for turn in speaker_turns:
        idx = [i for i, s in enumerate(samples) if turn["start"] <= s["t"] < turn["end"]]
        if not idx:
            continue
        motion = {t: 0.0 for t in real_tracks}
        for i in idx:
            for f in samples[i]["faces"]:
                if f["track"] in motion:
                    motion[f["track"]] += f["mouth"]
        speaker_track = max(motion, key=motion.get)
        if motion[speaker_track] <= 0:
            continue
        for i in idx:
            face = next((f for f in samples[i]["faces"] if f["track"] == speaker_track), None)
            if face is not None:
                out[i] = face
    return out


# --- camera path -------------------------------------------------------------

def smooth_path(
    targets: list[Optional[tuple[float, float]]],
    crop_w: float,
    dead_zone: float = DEAD_ZONE,
    jump: float = JUMP,
    alpha: float = EMA_ALPHA,
    hold: int = JUMP_HOLD,
    window: int = SMOOTH_WINDOW,
) -> list[tuple[float, float]]:
    """Turn per-sample target centers into a steady camera path. Gaps
    (None) hold the last target. Moves smaller than dead_zone * crop_w are
    ignored; moves bigger than jump * crop_w that persist for `hold`
    samples hard-cut; everything else eases in with an EMA, then a centered
    moving average smooths each run between hard cuts."""
    first = next((t for t in targets if t is not None), None)
    if first is None:
        return []
    filled, last = [], first
    for t in targets:
        last = t if t is not None else last
        filled.append(last)

    dz, jp = dead_zone * crop_w, jump * crop_w
    cur = list(first)
    raw, cut_at = [], set()
    pending = 0
    for i, (tx, ty) in enumerate(filled):
        dist = max(abs(tx - cur[0]), abs(ty - cur[1]))
        if dist > jp:
            pending += 1
            if pending >= hold:
                cur = [tx, ty]
                cut_at.add(i)
                pending = 0
        else:
            pending = 0
            if dist > dz:
                cur[0] += alpha * (tx - cur[0])
                cur[1] += alpha * (ty - cur[1])
        raw.append((cur[0], cur[1]))

    # Moving average within each run, never across a hard cut.
    out = []
    runs, run_start = [], 0
    for i in range(1, len(raw) + 1):
        if i == len(raw) or i in cut_at:
            runs.append((run_start, i))
            run_start = i
    half = window // 2
    for a, b in runs:
        for i in range(a, b):
            lo, hi = max(a, i - half), min(b, i + half + 1)
            xs = [raw[j][0] for j in range(lo, hi)]
            ys = [raw[j][1] for j in range(lo, hi)]
            out.append((sum(xs) / len(xs), sum(ys) / len(ys)))
    return out


def compute_crop_path(
    filepath: str,
    start: float,
    end: float,
    aspect: tuple[int, int] = (9, 16),
    speaker_turns: Optional[list[dict]] = None,
) -> Optional[dict]:
    """The crop window's top-left corner over time, or None when there's
    no usable face (caller falls back to a center crop).
    Returns {"crop_w", "crop_h", "src_w", "src_h", "fps", "times", "xy", "median_cx"}."""
    data = detect_faces(filepath, start, end)
    samples = data["samples"]
    if not samples:
        return None
    with_faces = sum(1 for s in samples if s["faces"])
    if with_faces < MIN_FACE_COVERAGE * len(samples):
        return None

    src_w, src_h = data["src_w"], data["src_h"]
    crop_w, crop_h = crop_size(src_w, src_h, *aspect)
    assign_tracks(samples, src_w)
    chosen = choose_targets(samples, speaker_turns)
    centers = smooth_path([(f["cx"], f["cy"]) if f else None for f in chosen], crop_w)
    xy = [clamp_window(cx, cy, src_w, src_h, crop_w, crop_h) for cx, cy in centers]
    picked_cx = [f["cx"] for f in chosen if f]
    return {
        "crop_w": crop_w, "crop_h": crop_h, "src_w": src_w, "src_h": src_h, "fps": data["fps"],
        "times": [s["t"] for s in samples], "xy": xy,
        "median_cx": float(np.median(picked_cx)) if picked_cx else src_w / 2,
    }


def write_sendcmd(path: dict, duration: float, out_path: str, target: str = "crop@rf") -> str:
    """One crop x/y command per output frame, linearly interpolated from
    the sampled path, so the pan moves every frame rather than in 5fps steps."""
    times = np.array(path["times"])
    xs = np.array([p[0] for p in path["xy"]])
    ys = np.array([p[1] for p in path["xy"]])
    n = max(1, int(np.ceil(duration * path["fps"])))
    frame_t = np.arange(n) / path["fps"]
    fx = np.interp(frame_t, times, xs)
    fy = np.interp(frame_t, times, ys)
    with open(out_path, "w", encoding="utf-8") as f:
        for t, x, y in zip(frame_t, fx, fy):
            f.write(f"{t:.4f} {target} x {x:.1f}, {target} y {y:.1f};\n")
    return out_path


def _escape_filter_path(p: str) -> str:
    return p.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


def center_crop_filter(aspect: tuple[int, int] = (9, 16)) -> str:
    aw, ah = aspect
    return (f"crop='min(iw,ih*{aw}/{ah})':'min(ih,iw*{ah}/{aw})':"
            f"'(iw-min(iw,ih*{aw}/{ah}))/2':'(ih-min(ih,iw*{ah}/{aw}))/2'")


def crop_filter(
    filepath: str,
    start: float,
    end: float,
    work_dir: str,
    tag: str,
    aspect: tuple[int, int] = (9, 16),
    speaker_turns: Optional[list[dict]] = None,
) -> tuple[str, list[str]]:
    """ffmpeg filter chain that crops [start, end) of filepath (input
    trimmed with -ss start, so stream time 0 is `start`) to `aspect`,
    following the speaker. Returns (filter, files_to_clean_up). Falls back
    to a plain center crop on no faces or any error."""
    try:
        path = compute_crop_path(filepath, start, end, aspect, speaker_turns)
    except Exception:
        logger.exception(f"reframe failed for {filepath} [{start:.1f}-{end:.1f}]; using center crop")
        path = None
    if path is None:
        return center_crop_filter(aspect), []

    os.makedirs(work_dir, exist_ok=True)
    cmd_path = os.path.join(work_dir, f"{tag}_reframe.cmd")
    write_sendcmd(path, end - start, cmd_path)
    x0, y0 = path["xy"][0]
    chain = (f"sendcmd=f={_escape_filter_path(cmd_path)},"
             f"crop@rf=w={path['crop_w']}:h={path['crop_h']}:x={x0:.1f}:y={y0:.1f}")
    return chain, [cmd_path]


def median_face_offset(filepath: str, start: float, end: float) -> Optional[tuple[float, int, int]]:
    """(median followed-face x, src_w, src_h) for a static framing, e.g.
    Resolve's Pan where keyframes can't be set from the scripting API."""
    try:
        path = compute_crop_path(filepath, start, end)
    except Exception:
        logger.exception("median face offset failed")
        return None
    if path is None:
        return None
    return path["median_cx"], path["src_w"], path["src_h"]


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    src, a, b = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    p = compute_crop_path(src, a, b)
    if p is None:
        print("no faces; center crop")
    else:
        xs = [x for x, _ in p["xy"]]
        print(f"crop {p['crop_w']}x{p['crop_h']} of {p['src_w']}x{p['src_h']}, "
              f"x range {min(xs):.0f}-{max(xs):.0f}, median face cx {p['median_cx']:.0f}")
