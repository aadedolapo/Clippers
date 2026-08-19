"""
ASS caption generation + ffmpeg burn-in (PRD section 6): word-by-word karaoke
highlight captions, emphasis words visually distinct, swappable style presets.
"""

import os
import subprocess

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output")

# Named style presets (PRD 6.4): font, colors, outline. ASS colors are &HAABBGGRR.
STYLE_PRESETS = {
    "bold-white": {
        "fontname": "Arial Black",
        "fontsize": 20,
        "primary_colour": "&H0000FFFF",   # sung/highlighted words: yellow
        "secondary_colour": "&H00FFFFFF",  # unsung words: white
        "outline_colour": "&H00000000",   # black outline
        "back_colour": "&H80000000",
        "bold": -1,
        "outline": 3,
        "shadow": 0,
        "alignment": 2,
        "margin_v": 90,
    },
    "yellow-pop": {
        "fontname": "Helvetica Neue",
        "fontsize": 22,
        "primary_colour": "&H0000D7FF",   # sung: orange
        "secondary_colour": "&H00FFFFFF",  # unsung: white
        "outline_colour": "&H00000000",
        "back_colour": "&H80000000",
        "bold": -1,
        "outline": 2.5,
        "shadow": 1,
        "alignment": 2,
        "margin_v": 100,
    },
    "minimal": {
        "fontname": "Helvetica Neue",
        "fontsize": 18,
        "primary_colour": "&H00E0E0E0",   # sung: near-white
        "secondary_colour": "&H00909090",  # unsung: gray
        "outline_colour": "&H00000000",
        "back_colour": "&H80000000",
        "bold": 0,
        "outline": 1.5,
        "shadow": 0,
        "alignment": 2,
        "margin_v": 80,
    },
}

DEFAULT_PRESET = "bold-white"
EMPHASIS_SCALE = 130  # % font scale for emphasis words
EMPHASIS_COLOUR = "&H0000FFFF"  # yellow pop, overridden per preset if it matches primary
FIRST_CAPTION_DELAY = 0.05  # seconds; near-zero so text appears almost immediately

# Output is center-cropped landscape -> 9:16 for Shorts/TikTok/Reels delivery.
# STYLE_PRESETS' fontsize/outline/shadow/margin_v are tuned against this
# reference height; _build_ass_header scales them to the actual output canvas.
PORTRAIT_WIDTH = 1080
PORTRAIT_HEIGHT = 1920
STYLE_REFERENCE_HEIGHT = 720


def _fmt_ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _build_ass_header(preset: dict) -> str:
    scale = PORTRAIT_HEIGHT / STYLE_REFERENCE_HEIGHT
    fontsize = round(preset["fontsize"] * scale)
    outline = round(preset["outline"] * scale, 1)
    shadow = round(preset["shadow"] * scale, 1)
    margin_v = round(preset["margin_v"] * scale)
    margin_lr = round(20 * scale)
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {PORTRAIT_WIDTH}
PlayResY: {PORTRAIT_HEIGHT}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{preset['fontname']},{fontsize},{preset['primary_colour']},{preset['secondary_colour']},{preset['outline_colour']},{preset['back_colour']},{preset['bold']},0,0,0,100,100,0,0,1,{outline},{shadow},{preset['alignment']},{margin_lr},{margin_lr},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _segment_words_in_range(segments: list[dict], start: float, end: float) -> list[dict]:
    words = []
    for seg in segments:
        for w in seg["words"]:
            if w["end"] <= start or w["start"] >= end:
                continue
            words.append(w)
    return words


def _group_into_lines(words: list[dict], max_words_per_line: int = 6) -> list[list[dict]]:
    lines, current = [], []
    for w in words:
        current.append(w)
        if len(current) >= max_words_per_line:
            lines.append(current)
            current = []
    if current:
        lines.append(current)
    return lines


def _karaoke_text(line_words: list[dict], clip_start: float, emphasis_set: set) -> str:
    parts = []
    for i, w in enumerate(line_words):
        word_start = w["start"] - clip_start
        word_end = w["end"] - clip_start
        if i + 1 < len(line_words):
            next_start = line_words[i + 1]["start"] - clip_start
        else:
            next_start = word_end
        duration_cs = max(1, round((next_start - word_start) * 100))

        text = w["word"]
        is_emphasis = text.strip().strip(".,!?\"'").lower() in emphasis_set
        if is_emphasis:
            parts.append(f"{{\\k{duration_cs}\\fscx{EMPHASIS_SCALE}\\fscy{EMPHASIS_SCALE}}}{text} {{\\r}}")
        else:
            parts.append(f"{{\\k{duration_cs}}}{text} ")
    return "".join(parts)


def generate_ass(
    segments: list[dict],
    start: float,
    end: float,
    emphasis_words: list[str],
    preset_name: str = DEFAULT_PRESET,
) -> str:
    """Build ASS subtitle content for the clip [start, end), word-by-word karaoke highlight."""
    preset = STYLE_PRESETS.get(preset_name, STYLE_PRESETS[DEFAULT_PRESET])
    # emphasis_words are phrases (e.g. "$240 a month"); split into individual
    # words since karaoke tagging operates on one transcript word at a time.
    emphasis_set = {
        token.strip(".,!?\"'").lower()
        for phrase in emphasis_words
        for token in phrase.split()
    }

    words = _segment_words_in_range(segments, start, end)
    if words and words[0]["start"] - start < FIRST_CAPTION_DELAY:
        words[0] = {**words[0], "start": start + FIRST_CAPTION_DELAY}

    lines = _group_into_lines(words)

    ass = _build_ass_header(preset)
    for line_words in lines:
        line_start = max(0.0, line_words[0]["start"] - start)
        line_end = max(line_start, line_words[-1]["end"] - start)
        text = _karaoke_text(line_words, start, emphasis_set)
        ass += f"Dialogue: 0,{_fmt_ass_time(line_start)},{_fmt_ass_time(line_end)},Default,,0,0,0,,{text}\n"

    return ass


def burn_captions(
    filepath: str,
    segments: list[dict],
    start: float,
    end: float,
    emphasis_words: list[str],
    preset_name: str = DEFAULT_PRESET,
    output_dir: str = OUTPUT_DIR,
) -> str:
    """Trim [start, end) from filepath, burn word-by-word ASS captions, write to output_dir."""
    os.makedirs(output_dir, exist_ok=True)

    base = os.path.splitext(os.path.basename(filepath))[0]
    tag = f"{base}_{start:.1f}-{end:.1f}_{preset_name}"
    ass_path = os.path.join(output_dir, f"{tag}.ass")
    out_path = os.path.join(output_dir, f"{tag}_captioned.mp4")

    ass_content = generate_ass(segments, start, end, emphasis_words, preset_name)
    with open(ass_path, "w") as f:
        f.write(ass_content)

    duration = end - start
    # ass_path may contain characters ffmpeg's filtergraph parser treats specially;
    # escape for use inside the -vf ass=... argument.
    escaped_ass_path = ass_path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

    # Center-crop landscape source to 9:16 (discards left/right, keeps full height),
    # then scale to the portrait delivery resolution before burning captions.
    crop = "crop=ih*9/16:ih:(iw-ih*9/16)/2:0"
    scale = f"scale={PORTRAIT_WIDTH}:{PORTRAIT_HEIGHT}"
    vf = f"{crop},{scale},ass={escaped_ass_path}"

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start), "-t", str(duration), "-i", filepath,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg caption burn-in failed: {result.stderr[-2000:]}")

    return out_path


if __name__ == "__main__":
    import sys

    sys.path.insert(0, os.path.dirname(__file__))
    from pipeline import transcribe

    path = sys.argv[1] if len(sys.argv) > 1 else "downloads/lgaR7Z9okYM.mp4"
    print(f"Transcribing {path} ...")
    segments = transcribe(path, model_size="small")

    # Hardcoded test clip + emphasis words (mirrors what llm_scoring would return).
    start, end = 40.0, 60.0
    emphasis_words = ["$240 a month", "Gone", "never looked"]

    for preset_name in STYLE_PRESETS:
        print(f"Burning captions with preset '{preset_name}' ...")
        out_path = burn_captions(path, segments, start, end, emphasis_words, preset_name=preset_name)
        print(f"  -> {out_path}")
