"""
Commentary bookend assembly (planned pipeline steps 3-5): renders TTS
narration into a captioned portrait video segment, then bookends the sermon
clip with it — commentary intro -> sermon clip -> commentary close — as one
final deliverable mp4. This produces the ffmpeg-rendered output
(captions.py's burn_captions path); the raw clip is still sent to the
Resolve timeline separately for manual polishing, unaffected by this module.
"""

import os
import subprocess
from typing import Optional

import captions
import tts

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output")
CROP = "crop=ih*9/16:ih:(iw-ih*9/16)/2:0"


def _run_ffmpeg(cmd: list[str], what: str) -> None:
    # ffmpeg's stderr can echo back non-ASCII bytes (the commentary text
    # embedded in the ASS filter path/content, font names, etc.) — without an
    # explicit encoding, subprocess falls back to the process locale, which
    # is plain ASCII in a bare shell and raises UnicodeDecodeError on capture.
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg {what} failed: {result.stderr[-2000:]}")


def _probe_duration(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return float(result.stdout.strip())


def _extract_frame(source_path: str, timestamp: float, out_path: str) -> str:
    _run_ffmpeg(
        ["ffmpeg", "-y", "-ss", str(max(0.0, timestamp)), "-i", source_path,
         "-frames:v", "1", "-f", "image2", out_path],
        "frame extraction",
    )
    return out_path


def _render_background(frame_path: str, duration: float, out_path: str) -> str:
    """A blurred, darkened freeze-frame from the source video, scaled to the
    same portrait canvas as the sermon clip — ties the commentary segment
    visually to its source without needing any brand asset from the user."""
    vf = f"{CROP},scale={captions.PORTRAIT_WIDTH}:{captions.PORTRAIT_HEIGHT},boxblur=20:2,eq=brightness=-0.15"
    _run_ffmpeg(
        ["ffmpeg", "-y", "-loop", "1", "-i", frame_path, "-t", str(duration),
         "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", out_path],
        "background render",
    )
    return out_path


def render_commentary_segment(
    text: str,
    source_path: str,
    frame_timestamp: float,
    output_dir: str,
    tag: str,
) -> dict:
    """Synthesize `text` as narration, caption it (distinct style from the
    sermon clip), and composite it over a blurred freeze-frame from
    source_path at frame_timestamp. Returns {"path": <segment mp4>,
    "intermediates": [audio, frame, background, ass paths]} — the segment
    mp4 itself is excluded from "intermediates" since callers may hand it to
    Resolve (which references it by path rather than copying it), so it must
    outlive this function unlike the scratch files that feed it."""
    os.makedirs(output_dir, exist_ok=True)

    audio_path = os.path.join(output_dir, f"{tag}_audio.mp3")
    tts_result = tts.synthesize_with_timestamps(text, audio_path)
    duration = tts_result["duration"] or 4.0  # fallback if alignment came back empty

    frame_path = os.path.join(output_dir, f"{tag}_frame.png")
    _extract_frame(source_path, frame_timestamp, frame_path)

    bg_path = os.path.join(output_dir, f"{tag}_bg.mp4")
    _render_background(frame_path, duration, bg_path)

    ass_content = captions.generate_commentary_ass(tts_result["words"])
    ass_path = os.path.join(output_dir, f"{tag}.ass")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass_content)
    escaped_ass_path = ass_path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

    out_path = os.path.join(output_dir, f"{tag}_segment.mp4")
    _run_ffmpeg(
        ["ffmpeg", "-y", "-i", bg_path, "-i", audio_path,
         "-vf", f"ass={escaped_ass_path}",
         "-c:v", "libx264", "-preset", "fast", "-crf", "20",
         "-c:a", "aac", "-b:a", "128k", "-shortest",
         "-map", "0:v:0", "-map", "1:a:0", out_path],
        "commentary segment composite",
    )
    return {"path": out_path, "intermediates": [audio_path, frame_path, bg_path, ass_path]}


def concat_segments(paths: list[str], out_path: str) -> str:
    """Concatenate 2+ mp4s (matching resolution/codec) into one file via the
    concat filter (re-encodes, so it tolerates minor encoder param drift
    between segments — unlike the concat demuxer's stream-copy requirement)."""
    if len(paths) < 2:
        raise ValueError("concat_segments needs at least 2 paths")

    inputs = []
    filter_inputs = []
    for i, p in enumerate(paths):
        inputs += ["-i", p]
        filter_inputs.append(f"[{i}:v:0][{i}:a:0]")
    filter_complex = "".join(filter_inputs) + f"concat=n={len(paths)}:v=1:a=1[outv][outa]"

    _run_ffmpeg(
        # Segments can enter with different native frame rates (the TTS
        # background is fixed at 30fps, the sermon clip inherits its source's
        # fps) — the concat filter tolerates that by interpolating, but
        # forcing -r here normalizes the output instead of leaving it at
        # whatever blended rate the interpolation lands on.
        ["ffmpeg", "-y", *inputs, "-filter_complex", filter_complex,
         "-map", "[outv]", "-map", "[outa]",
         "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-r", "30",
         "-c:a", "aac", "-b:a", "128k", out_path],
        "concat",
    )
    return out_path


def concat_with_crossfade(path_a: str, path_b: str, out_path: str, crossfade: float = 0.15) -> str:
    """Join two mp4s with a short synchronized audio+video crossfade, instead
    of concat_segments' hard cut — used for the cold-open "flash-forward"
    (retention upgrade plan phase 3), where a jarring hard cut back into
    material the viewer may have just heard would undercut the effect.
    xfade/acrossfade (rather than the concat filter) keep video and audio in
    sync through the transition: both produce an output of duration
    dur_a + dur_b - crossfade, with xfade's offset anchored to path_a's own
    probed duration so the transition starts exactly `crossfade` seconds
    before its end regardless of that duration."""
    dur_a = _probe_duration(path_a)
    offset = max(0.0, dur_a - crossfade)
    filter_complex = (
        f"[0:v][1:v]xfade=transition=fade:duration={crossfade}:offset={offset}[v];"
        f"[0:a][1:a]acrossfade=d={crossfade}[a]"
    )
    _run_ffmpeg(
        ["ffmpeg", "-y", "-i", path_a, "-i", path_b, "-filter_complex", filter_complex,
         "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-preset", "fast", "-crf", "20",
         "-c:a", "aac", "-b:a", "128k", out_path],
        "cold open crossfade",
    )
    return out_path


def build_clip_with_cold_open(
    filepath: str,
    segments: list[dict],
    cold_open: dict,
    start: float,
    end: float,
    emphasis_words: list[str],
    output_dir: str = OUTPUT_DIR,
    preset_name: str = captions.DEFAULT_PRESET,
    pinned_line: Optional[str] = None,
    pinned_line_emphasis: Optional[list[str]] = None,
    crossfade: float = 0.15,
) -> str:
    """Render the cold-open bumper (cold_open["start"]/["end"]) and the main
    clip [start, end) as two independently captioned parts, then join them
    with a short crossfade — the deliverable mp4 for a clip using a cold
    open. The cold-open range may fall inside [start, end): that overlap is
    intentional (the strongest line plays once as the bumper, then again in
    its natural place in the main clip), so the main clip is always rendered
    in full regardless of where cold_open sits."""
    os.makedirs(output_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(filepath))[0]
    tag_root = f"{base}_{start:.1f}-{end:.1f}"

    cold_path = captions.burn_captions(
        filepath, segments, cold_open["start"], cold_open["end"], emphasis_words,
        preset_name=preset_name, output_dir=output_dir,
    )
    cold_tag = captions.clip_tag(filepath, cold_open["start"], cold_open["end"], preset_name)
    cold_ass_path = os.path.join(output_dir, f"{cold_tag}.ass")

    main_path = captions.burn_captions(
        filepath, segments, start, end, emphasis_words, preset_name=preset_name, output_dir=output_dir,
        pinned_line=pinned_line, pinned_line_emphasis=pinned_line_emphasis,
    )
    main_tag = captions.clip_tag(filepath, start, end, preset_name)
    intermediates = [cold_path, cold_ass_path, main_path, os.path.join(output_dir, f"{main_tag}.ass")]
    if pinned_line:
        intermediates.append(os.path.join(output_dir, f"{main_tag}_pinned.ass"))

    final_path = os.path.join(output_dir, f"{tag_root}_coldopen.mp4")
    concat_with_crossfade(cold_path, main_path, final_path, crossfade=crossfade)
    cleanup_intermediates(intermediates)
    return final_path


def build_clip_parts(
    filepath: str,
    segments: list[dict],
    start: float,
    end: float,
    emphasis_words: list[str],
    commentary_intro: str,
    commentary_close: str,
    output_dir: str = OUTPUT_DIR,
    preset_name: str = captions.DEFAULT_PRESET,
    pinned_line: Optional[str] = None,
    pinned_line_emphasis: Optional[list[str]] = None,
) -> dict:
    """Render the sermon clip (captioned) and, if present, the commentary
    intro/close segments — WITHOUT concatenating. One TTS call each, so
    callers needing the pieces for more than one destination (the standalone
    deliverable mp4 via concat_parts, and the Resolve timeline via
    resolve_integration.send_clip_with_commentary_to_resolve) share a single
    render rather than paying for narration twice. Returns {"sermon": path,
    "intro": path|None, "close": path|None, "intermediates": [...]} — the
    last is every scratch file safe to delete once both the Resolve send and
    concat_parts have finished with this result (see cleanup_intermediates).

    pinned_line/pinned_line_emphasis are passed straight through to
    captions.burn_captions — the persistent top-pinned "strongest line"
    overlay, burned into the sermon clip alongside the bottom karaoke
    captions. No-op when pinned_line is empty."""
    os.makedirs(output_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(filepath))[0]
    tag_root = f"{base}_{start:.1f}-{end:.1f}"

    sermon_path = captions.burn_captions(
        filepath, segments, start, end, emphasis_words, preset_name=preset_name, output_dir=output_dir,
        pinned_line=pinned_line, pinned_line_emphasis=pinned_line_emphasis,
    )
    # captions.burn_captions computes these same paths internally but only
    # returns the mp4 — recomputed here via its shared naming helper rather
    # than duplicating the formula.
    sermon_tag = captions.clip_tag(filepath, start, end, preset_name)
    sermon_ass_path = os.path.join(output_dir, f"{sermon_tag}.ass")
    intermediates = [sermon_ass_path]
    if pinned_line:
        intermediates.append(os.path.join(output_dir, f"{sermon_tag}_pinned.ass"))

    intro_path = None
    if commentary_intro:
        intro_result = render_commentary_segment(commentary_intro, filepath, start, output_dir, f"{tag_root}_intro")
        intro_path = intro_result["path"]
        intermediates += intro_result["intermediates"]

    close_path = None
    if commentary_close:
        close_result = render_commentary_segment(commentary_close, filepath, max(0.0, end - 0.5), output_dir, f"{tag_root}_close")
        close_path = close_result["path"]
        intermediates += close_result["intermediates"]

    return {"sermon": sermon_path, "intro": intro_path, "close": close_path, "intermediates": intermediates}


def cleanup_intermediates(intermediates: list[str]) -> None:
    """Delete scratch files (TTS audio, extracted frame, blurred background,
    ass captions) once they've been folded into something durable — never
    call this on the segment mp4s themselves: those may already be
    referenced by a Resolve timeline (Resolve links to the file path rather
    than copying it), so deleting them would leave offline media behind."""
    for path in intermediates:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def concat_parts(parts: dict, filepath: str, start: float, end: float, output_dir: str = OUTPUT_DIR) -> str:
    """Assemble a build_clip_parts() result into one deliverable mp4 —
    intro -> sermon -> close, skipping whichever bookend wasn't rendered."""
    ordered = [p for p in (parts.get("intro"), parts["sermon"], parts.get("close")) if p]
    if len(ordered) == 1:
        return ordered[0]

    base = os.path.splitext(os.path.basename(filepath))[0]
    tag_root = f"{base}_{start:.1f}-{end:.1f}"
    final_path = os.path.join(output_dir, f"{tag_root}_bookended.mp4")
    return concat_segments(ordered, final_path)


def build_bookended_clip(
    filepath: str,
    segments: list[dict],
    start: float,
    end: float,
    emphasis_words: list[str],
    commentary_intro: str,
    commentary_close: str,
    output_dir: str = OUTPUT_DIR,
    preset_name: str = captions.DEFAULT_PRESET,
    pinned_line: Optional[str] = None,
    pinned_line_emphasis: Optional[list[str]] = None,
) -> str:
    """Full pipeline: render the parts, then concatenate into one deliverable
    mp4. Falls back to the plain captioned clip if neither commentary field
    is set."""
    parts = build_clip_parts(
        filepath, segments, start, end, emphasis_words, commentary_intro, commentary_close, output_dir, preset_name,
        pinned_line=pinned_line, pinned_line_emphasis=pinned_line_emphasis,
    )
    final_path = concat_parts(parts, filepath, start, end, output_dir)
    cleanup_intermediates(parts["intermediates"])
    return final_path


if __name__ == "__main__":
    import sys

    sys.path.insert(0, os.path.dirname(__file__))
    from pipeline import transcribe

    path = sys.argv[1] if len(sys.argv) > 1 else "downloads/lgaR7Z9okYM.mp4"
    print(f"Transcribing {path} ...")
    segments = transcribe(path, model_size="small")

    start, end = 40.0, 60.0
    intro = "Here's a moment that reframes what winning actually costs."
    close = "Worth sitting with: what are you optimizing for without noticing?"

    print("Building bookended clip ...")
    out_path = build_bookended_clip(path, segments, start, end, [], intro, close)
    print(f"  -> {out_path}")
