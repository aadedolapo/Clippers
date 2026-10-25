"""
ASS caption generation + ffmpeg burn-in: word-by-word karaoke
highlight captions, emphasis words visually distinct, swappable style presets.
"""

import os
import re
import subprocess
from typing import Optional

from clippers import paths, reframe

OUTPUT_DIR = paths.OUTPUT_DIR

# Named style presets: font, colors, outline. ASS colors are &HAABBGGRR.
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

# Commentary bookends get their own style, deliberately distinct from
# STYLE_PRESETS above — a different visual identity signals to the viewer
# that this is original spoken commentary, not part of the reused clip.
COMMENTARY_STYLE_PRESETS = {
    "commentary-clean": {
        "fontname": "Helvetica Neue",
        "fontsize": 24,
        "primary_colour": "&H00E6C229",   # sung: teal/cyan
        "secondary_colour": "&H00FFFFFF",  # unsung: white
        "outline_colour": "&H00000000",
        "back_colour": "&H80000000",
        "bold": -1,
        "outline": 2.5,
        "shadow": 1,
        "alignment": 5,   # middle-center, vs. the sermon captions' bottom alignment
        "margin_v": 0,
    },
}
DEFAULT_COMMENTARY_PRESET = "commentary-clean"

# The persistent top-pinned "strongest line" overlay — a single static line
# for the clip's full duration, distinct from both the bottom karaoke
# captions and the commentary style (top-aligned instead of bottom/middle).
PINNED_LINE_STYLE = {
    "fontname": "Arial Black",
    "fontsize": 24,
    "primary_colour": "&H00FFFFFF",   # white — no karaoke reveal on this line, so no "unsung" state
    "secondary_colour": "&H00FFFFFF",
    "outline_colour": "&H00000000",
    "back_colour": "&H80000000",
    "bold": -1,
    "outline": 3,
    "shadow": 0,
    "alignment": 8,   # top-center
    "margin_v": 90,   # distance from the TOP edge (alignment=8 measures margin_v from the top)
}
# ASS colours are &HBBGGRR — pure red is &H0000FF (blue=00, green=00, red=FF).
PINNED_EMPHASIS_COLOUR = "&H0000FF&"

# The trailing comment-bait question overlay — bottom-center (distinct from
# pinned_line's top-center) so the two never collide when both are present,
# and only shown for the last COMMENT_BAIT_TRAILING_SECONDS of the clip.
COMMENT_BAIT_STYLE = {
    "fontname": "Helvetica Neue",
    "fontsize": 26,
    "primary_colour": "&H00FFFFFF",
    "secondary_colour": "&H00FFFFFF",
    "outline_colour": "&H00000000",
    "back_colour": "&H80000000",
    "bold": -1,
    "outline": 3,
    "shadow": 0,
    "alignment": 2,   # bottom-center
    "margin_v": 140,
}
COMMENT_BAIT_TRAILING_SECONDS = 2.5

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

# Render targets. Width stays 1080 for all of them, so
# caption font sizes (in ASS PlayRes units) keep the same size relative to
# the frame width; only the canvas height changes.
ASPECTS = {
    "9:16": (PORTRAIT_WIDTH, PORTRAIT_HEIGHT),
    "4:5": (PORTRAIT_WIDTH, 1350),
    "1:1": (PORTRAIT_WIDTH, 1080),
}
DEFAULT_ASPECT = "9:16"


def aspect_dims(aspect: str) -> tuple[int, int]:
    return ASPECTS.get(aspect, ASPECTS[DEFAULT_ASPECT])


def _aspect_ratio(aspect: str) -> tuple[int, int]:
    w, h = aspect.split(":") if aspect in ASPECTS else DEFAULT_ASPECT.split(":")
    return int(w), int(h)



# "Minimal Faith" style: phrase-based animated captions + edit effects — see
# generate_minimal_faith_ass and render_minimal_faith_clip below. Not a
# STYLE_PRESETS entry: it needs phrase grouping and entrance animations, not
# per-word karaoke, so it gets its own generation path entirely.
MINIMAL_FAITH_PRESET = "minimal-faith"
FONTS_DIR = paths.FONTS_DIR

MINIMAL_FAITH_BODY_FONT = "Montserrat ExtraBold"
MINIMAL_FAITH_BODY_SIZE = 96
MINIMAL_FAITH_EMPH_FONT = "Playfair Display"
MINIMAL_FAITH_EMPH_SIZE = 122
MINIMAL_FAITH_EMPH_COLOUR = "&H4DD8FF&"  # #FFD84D in ASS BGR
MINIMAL_FAITH_POS_Y = 1440
MINIMAL_FAITH_MAX_LINE_CHARS = 16

MINIMAL_FAITH_ZOOM_FROM = 1.12
MINIMAL_FAITH_ZOOM_SECS = 1.7
MINIMAL_FAITH_VOICE_LUFS = -15
# Confirmed via listening test during the style-test run (2026-09-25): the
# prototype's original -33 (18 LU under the voice) read as too quiet; -28
# (13 LU under) was confirmed as the right level.
MINIMAL_FAITH_MUSIC_LUFS = -28
MINIMAL_FAITH_MUSIC_OFFSET = 6.0
MINIMAL_FAITH_WHOOSH_VOLUME = 0.35
MINIMAL_FAITH_WHOOSH_MAX_SECS = 1.5
MINIMAL_FAITH_RISER_VOLUME = 0.4

# Words too generic to ever be the one highlighted word in a phrase, and a
# signal (alongside punctuation/gaps) that a phrase can safely extend one
# more word rather than breaking on a weak word. Ported verbatim from the
# prototype.
_STOP_WORDS = {"a", "an", "the", "to", "of", "and", "or", "in", "on", "that",
               "you", "i", "it", "is", "be", "me", "do", "we", "they"}

# ASS override-tag animations, ported verbatim from the prototype: "up" for
# the phrase carrying the emphasis word, otherwise alternating
# blur_slide_up/slide_right so consecutive plain phrases don't feel
# repetitive.
def _minimal_faith_anims(pos_y: int = MINIMAL_FAITH_POS_Y) -> dict:
    return {
        "up": "{\\an5\\pos(540,%d)\\fscx70\\fscy70\\fad(100,60)\\t(0,180,0.5,\\fscx100\\fscy100)}" % pos_y,
        "blur_slide_up": "{\\an5\\move(540,%d,540,%d,0,200)\\blur10\\fad(120,60)\\t(0,200,0.5,\\blur0.6)}" % (pos_y + 45, pos_y),
        "slide_right": "{\\an5\\move(505,%d,540,%d,0,220)\\blur4\\fad(110,60)\\t(0,220,0.5,\\blur0.6)}" % (pos_y, pos_y),
    }


def _fmt_ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _build_ass_header(preset: dict, out_h: int = PORTRAIT_HEIGHT) -> str:
    # Scale is fixed to the portrait reference (not out_h) so text keeps its
    # size relative to the 1080px width on shorter 1:1/4:5 canvases.
    scale = PORTRAIT_HEIGHT / STYLE_REFERENCE_HEIGHT
    fontsize = round(preset["fontsize"] * scale)
    outline = round(preset["outline"] * scale, 1)
    shadow = round(preset["shadow"] * scale, 1)
    margin_v = round(preset["margin_v"] * scale)
    margin_lr = round(20 * scale)
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {PORTRAIT_WIDTH}
PlayResY: {out_h}
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
    out_h: int = PORTRAIT_HEIGHT,
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

    ass = _build_ass_header(preset, out_h)
    for line_words in lines:
        line_start = max(0.0, line_words[0]["start"] - start)
        line_end = max(line_start, line_words[-1]["end"] - start)
        text = _karaoke_text(line_words, start, emphasis_set)
        ass += f"Dialogue: 0,{_fmt_ass_time(line_start)},{_fmt_ass_time(line_end)},Default,,0,0,0,,{text}\n"

    return ass


def generate_commentary_ass(words: list[dict], preset_name: str = DEFAULT_COMMENTARY_PRESET) -> str:
    """Build ASS captions for a standalone TTS commentary segment. `words` are
    already timed from t=0 (the segment's own timeline, not a slice of a
    larger source) — shaped like whisper's word dicts ({"word","start","end"})
    so this reuses the same karaoke-highlight machinery as the sermon clip's
    captions, just with COMMENTARY_STYLE_PRESETS instead."""
    preset = COMMENTARY_STYLE_PRESETS.get(preset_name, COMMENTARY_STYLE_PRESETS[DEFAULT_COMMENTARY_PRESET])
    lines = _group_into_lines(words, max_words_per_line=5)

    ass = _build_ass_header(preset)
    for line_words in lines:
        if not line_words:
            continue
        line_start = max(0.0, line_words[0]["start"])
        line_end = max(line_start, line_words[-1]["end"])
        text = _karaoke_text(line_words, clip_start=0.0, emphasis_set=set())
        ass += f"Dialogue: 0,{_fmt_ass_time(line_start)},{_fmt_ass_time(line_end)},Default,,0,0,0,,{text}\n"

    return ass


def generate_static_overlay_ass(
    pinned_line: str,
    pinned_line_emphasis: list[str],
    duration: float,
    style: dict = PINNED_LINE_STYLE,
    start: float = 0.0,
    out_h: int = PORTRAIT_HEIGHT,
) -> str:
    """Build a standalone ASS file with ONE static dialogue line spanning
    [start, duration) — the single strongest verbatim line, pinned at the
    top of frame, by default for the whole clip (start=0.0). Unlike
    generate_ass's word-by-word karaoke reveal, this line is fully visible
    throughout its window; only the emphasis keywords are visually
    distinguished, via a red colour override instead of a reveal effect.

    start > 0 renders a TRAILING-only window instead (e.g. a comment-bait
    question shown only for the last few seconds) — the emphasis-colouring
    logic is identical either way, only the dialogue line's own timing
    changes."""
    # pinned_line_emphasis may be short phrases, not single words — split
    # into individual tokens for matching, same convention generate_ass uses
    # for emphasis_words.
    emphasis_set = {
        token.strip(".,!?\"'").lower()
        for phrase in pinned_line_emphasis
        for token in phrase.split()
    }

    parts = []
    for word in pinned_line.split():
        bare = word.strip(".,!?\"'").lower()
        if bare in emphasis_set:
            parts.append(f"{{\\c{PINNED_EMPHASIS_COLOUR}}}{word}{{\\r}}")
        else:
            parts.append(word)
    text = " ".join(parts)

    ass = _build_ass_header(style, out_h)
    ass += f"Dialogue: 0,{_fmt_ass_time(start)},{_fmt_ass_time(duration)},Default,,0,0,0,,{text}\n"
    return ass


def _build_minimal_faith_header(out_h: int = PORTRAIT_HEIGHT) -> str:
    # Not built via _build_ass_header: that function scales preset values
    # against STYLE_REFERENCE_HEIGHT for the karaoke presets, but the
    # minimal-faith values above are already tuned directly against the
    # 1080x1920 canvas (matching the prototype), so scaling them again
    # would blow up the sizing.
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {PORTRAIT_WIDTH}
PlayResY: {out_h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Body,{MINIMAL_FAITH_BODY_FONT},{MINIMAL_FAITH_BODY_SIZE},&H00FFFFFF,&H00FFFFFF,&H50000000,&H64000000,0,0,0,0,100,100,1,0,1,2.5,5,5,80,80,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _group_into_phrases(
    words: list[dict],
    emphasis_words: list[str],
    max_words: int = 4,
    max_dur: float = 1.4,
    gap: float = 0.30,
) -> list[list[dict]]:
    """Port of the prototype's phrases(): group words into 2-4 word phrases,
    breaking at punctuation (also treating a capitalised next word other
    than "I" as an implied sentence start, since Whisper often drops full
    stops), at gaps over `gap` seconds, or once a phrase would run past
    `max_dur` seconds — but never splitting a multi-word emphasis phrase
    (e.g. "horrible past") apart."""
    emphasis_lower = [e.lower() for e in emphasis_words if " " in e]
    out: list[list[dict]] = []
    cur: list[dict] = []
    for i, wd in enumerate(words):
        cur.append(wd)
        nxt = words[i + 1] if i + 1 < len(words) else None
        w_text = wd["word"].strip()
        nxt_text = nxt["word"].strip() if nxt else ""

        punct = bool(w_text) and w_text[-1] in ".,!?;:"
        if nxt and nxt_text[:1].isupper() and nxt_text != "I" and not nxt_text.startswith(("I ", "I'")):
            punct = True

        inside_emph = nxt is not None and any(
            f"{w_text} {nxt_text}".lower().strip(".,!?").startswith(e) or e in f"{w_text} {nxt_text}".lower()
            for e in emphasis_lower
        )

        brk = (
            nxt is None
            or punct
            or len(cur) >= max_words
            or (nxt["start"] - wd["end"]) > gap
            or (wd["end"] - cur[0]["start"]) > max_dur
        )
        # Don't end a phrase on a weak word if it can extend by one instead.
        if brk and nxt and not punct and len(cur) < max_words and w_text.lower() in _STOP_WORDS \
                and len(cur) >= 2 and (nxt["start"] - wd["end"]) <= gap:
            brk = len(cur) >= max_words + 1
        if inside_emph and nxt is not None and len(cur) < max_words + 1:
            brk = False

        if brk and (len(cur) >= 2 or punct or nxt is None or (nxt["start"] - wd["end"]) > gap):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return _merge_short_phrases(out, max_words)


def _merge_short_phrases(phrases: list[list[dict]], max_words: int) -> list[list[dict]]:
    """The punctuation-forced break above bypasses the len(cur) >= 2 check
    (by design, so a real sentence boundary still breaks immediately) —
    but that means a short residual buffer immediately followed by a
    capitalised word (common with back-to-back proper nouns, e.g. "...
    Stanford and Berkeley. And I...") can strand a lone word as its own
    phrase, and a run of several such words in a row (e.g. a rapid list of
    names) can strand more than one. Unconditionally merge any phrase under
    2 words into a neighbour — no cap on the resulting length — since an
    occasional longer phrase is a far smaller visual problem than a single
    word flashing on screen alone."""
    merged = [list(p) for p in phrases]
    i = 0
    while i < len(merged):
        if len(merged[i]) >= 2 or len(merged) == 1:
            i += 1
            continue
        if i > 0:
            merged[i - 1] = merged[i - 1] + merged[i]
            merged.pop(i)
            i -= 1
        else:
            merged[i + 1] = merged[i] + merged[i + 1]
            merged.pop(i)
    return merged


def _phrase_emph_index(phrase: list[dict], emphasis_words: list[str]) -> Optional[int]:
    """Index of the one word in `phrase` to visually highlight, or None if
    nothing in it matches emphasis_words. Ported from the prototype's
    emph_index()."""
    tokens = {t.lower() for e in emphasis_words for t in e.split()}
    best = None
    for i, wd in enumerate(phrase):
        clean = re.sub(r"[^\w']", "", wd["word"]).lower()
        if clean in tokens and clean not in _STOP_WORDS and clean != "too":
            best = i
    return best


def _wrap_phrase_words(phrase: list[dict], max_chars: int) -> list[list[dict]]:
    """Split a phrase onto a second line only if its plain text would run
    past max_chars — phrases are 2-4 words, so a simple word-count halving
    is a fine approximation of proper line-breaking here."""
    text_len = sum(len(w["word"].strip()) for w in phrase) + (len(phrase) - 1)
    if text_len <= max_chars or len(phrase) < 2:
        return [phrase]
    split = (len(phrase) + 1) // 2
    return [phrase[:split], phrase[split:]]


def generate_minimal_faith_ass(
    segments: list[dict],
    start: float,
    end: float,
    emphasis_words: list[str],
    capitalize_divine_pronouns: Optional[list[str]] = None,
    out_h: int = PORTRAIT_HEIGHT,
) -> str:
    """Build ASS content for the "minimal-faith" style: short animated
    phrases (2-4 words) instead of per-word karaoke, centered lower-middle,
    with at most one highlighted word per phrase, reading word timings
    directly from the Whisper segments.

    capitalize_divine_pronouns: an optional list of already-decided literal
    words (e.g. ["he", "his"]) to force-capitalize wherever they appear as a
    standalone token. The mechanism is wired up here but nothing currently
    populates this list — a "reverence rule" (capitalize He/Him/His when referring to God/Jesus) needs real
    language understanding of what each pronoun refers to, not a text
    heuristic, so deciding it was deliberately deferred rather than guessed
    at the risk of mis-capitalizing an unrelated "he"."""
    words = _segment_words_in_range(segments, start, end)
    if not words:
        return _build_minimal_faith_header(out_h)
    if words[0]["start"] - start < FIRST_CAPTION_DELAY:
        words[0] = {**words[0], "start": start + FIRST_CAPTION_DELAY}

    phrases = _group_into_phrases(words, emphasis_words)
    cap_set = {w.lower() for w in (capitalize_divine_pronouns or [])}
    # Same relative height (75% down) on any canvas.
    anims = _minimal_faith_anims(round(out_h * MINIMAL_FAITH_POS_Y / PORTRAIT_HEIGHT))

    ass = _build_minimal_faith_header(out_h)
    alt = 0
    for i, phrase in enumerate(phrases):
        p_start = max(0.0, phrase[0]["start"] - start)
        natural_end = phrase[-1]["end"] - start + 0.25
        next_start = (phrases[i + 1][0]["start"] - start) if i + 1 < len(phrases) else float("inf")
        p_end = max(p_start, min(natural_end, next_start))

        ei = _phrase_emph_index(phrase, emphasis_words)
        if ei is not None:
            anim = anims["up"]
        else:
            anim = anims["blur_slide_up" if alt % 2 == 0 else "slide_right"]
            alt += 1

        rendered_lines = []
        idx = 0
        for line_words in _wrap_phrase_words(phrase, MINIMAL_FAITH_MAX_LINE_CHARS):
            parts = []
            for wd in line_words:
                w = wd["word"].strip()
                bare = re.sub(r"[^\w']", "", w).lower()
                if bare in cap_set and w[:1].islower():
                    w = w[0].upper() + w[1:]
                if idx == ei:
                    parts.append(
                        f"{{\\fn{MINIMAL_FAITH_EMPH_FONT}\\i1\\b1\\fs{MINIMAL_FAITH_EMPH_SIZE}"
                        f"\\c{MINIMAL_FAITH_EMPH_COLOUR}}}{w}{{\\r}}\\h"
                    )
                else:
                    parts.append(w)
                idx += 1
            rendered_lines.append(" ".join(parts).rstrip(","))
        text = "\\N".join(rendered_lines)

        ass += f"Dialogue: 0,{_fmt_ass_time(p_start)},{_fmt_ass_time(p_end)},Body,,0,0,0,,{anim}{text}\n"

    return ass


def _first_emphasis_offset(
    segments: list[dict], start: float, end: float, emphasis_words: list[str]
) -> Optional[float]:
    """Seconds from `start` to the first word in range matching
    emphasis_words — used to time a riser SFX to end on the payoff line.
    None if no emphasis word actually falls in this clip's range."""
    emphasis_set = {t.strip(".,!?\"'").lower() for phrase in emphasis_words for t in phrase.split()}
    if not emphasis_set:
        return None
    for w in _segment_words_in_range(segments, start, end):
        bare = re.sub(r"[^\w']", "", w["word"]).lower()
        if bare in emphasis_set:
            return w["start"] - start
    return None


def render_minimal_faith_clip(
    filepath: str,
    segments: list[dict],
    start: float,
    end: float,
    emphasis_words: list[str],
    output_dir: str = OUTPUT_DIR,
    zoom_opener: bool = True,
    bw_grade: bool = False,
    music_path: Optional[str] = None,
    whoosh_path: Optional[str] = None,
    riser_path: Optional[str] = None,
    comment_bait_question: Optional[str] = None,
    add_captions: bool = True,
    aspect: str = DEFAULT_ASPECT,
    follow_speaker: bool = True,
    speaker_turns: Optional[list[dict]] = None,
) -> str:
    """Render the "minimal-faith" deliverable: generate_minimal_faith_ass's
    captions plus the edit effects from the prototype — a zoom-out opener,
    optional B&W grade, a ducked music bed, and up to two SFX (a whoosh on
    the opener, a riser ending on the first emphasis word). Reuses the
    existing generic center-crop (not the prototype's hardcoded 1920x1080
    pixel math, which assumed one fixed source resolution) so this works on
    any source size.

    music_path/whoosh_path/riser_path are None to skip that layer entirely
    — callers (main.py) resolve these via music.pick_track()/
    sfx.pick_whoosh()/sfx.pick_riser() before calling, so this function
    itself has no opinion on selection, only mixing.

    comment_bait_question, when given, burns a trailing text overlay (last
    COMMENT_BAIT_TRAILING_SECONDS) regardless of add_captions — see
    burn_captions' matching parameter for why it's independent.

    add_captions=False skips the karaoke captions, keeping the zoom/grade/
    music/SFX bundle — for callers who'll add their own subtitle template by
    hand in Resolve instead.

    aspect picks the canvas (ASPECTS). follow_speaker uses reframe.py's
    face-following crop instead of a fixed center crop; speaker_turns
    (diarize.py, clip-relative) lets it follow whoever is talking."""
    os.makedirs(output_dir, exist_ok=True)
    duration = end - start
    out_w, out_h = aspect_dims(aspect)

    tag = clip_tag(filepath, start, end, MINIMAL_FAITH_PRESET, aspect)
    out_path = os.path.join(output_dir, f"{tag}_captioned.mp4" if add_captions else f"{tag}.mp4")

    crop, cleanup = _crop_chain(filepath, start, end, output_dir, tag, aspect, follow_speaker, speaker_turns)
    vf = [crop, f"scale={out_w}:{out_h}:flags=lanczos"]
    if zoom_opener:
        zf = int(round(MINIMAL_FAITH_ZOOM_SECS * 29.97))
        z = f"1+{MINIMAL_FAITH_ZOOM_FROM - 1:.3f}*pow(1-min(on/{zf},1),3)"
        vf.append(
            f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:"
            f"s={out_w}x{out_h}:fps=30000/1001"
        )
    if bw_grade:
        vf.append("hue=s=0,eq=contrast=1.08")
    if add_captions:
        ass_path = os.path.join(output_dir, f"{tag}.ass")
        ass_content = generate_minimal_faith_ass(segments, start, end, emphasis_words, out_h=out_h)
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)
        escaped_ass_path = reframe.filter_path(ass_path)
        escaped_fonts_dir = reframe.filter_path(FONTS_DIR)
        vf.append(f"ass={escaped_ass_path}:fontsdir={escaped_fonts_dir}")

    # Independent of add_captions — see burn_captions' matching comment.
    if comment_bait_question:
        bait_ass_path = os.path.join(output_dir, f"{tag}_bait.ass")
        bait_start = max(0.0, duration - COMMENT_BAIT_TRAILING_SECONDS)
        bait_ass_content = generate_static_overlay_ass(
            comment_bait_question, [], duration, style=COMMENT_BAIT_STYLE, start=bait_start, out_h=out_h,
        )
        with open(bait_ass_path, "w", encoding="utf-8") as f:
            f.write(bait_ass_content)
        escaped_bait_ass_path = reframe.filter_path(bait_ass_path)
        vf.append(f"ass={escaped_bait_ass_path}")

    inputs = ["-ss", str(start), "-t", str(duration), "-i", filepath]
    filter_complex = [f"[0:v]{','.join(vf)}[v]"]
    voice_chain = (
        f"loudnorm=I={MINIMAL_FAITH_VOICE_LUFS}:TP=-1.5:LRA=11,"
        f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    )
    next_input_idx = 1

    if music_path and os.path.exists(music_path):
        filter_complex.append(f"[0:a]{voice_chain},asplit=2[voice][sc]")
        inputs += ["-ss", str(MINIMAL_FAITH_MUSIC_OFFSET), "-t", str(duration), "-i", music_path]
        music_idx, next_input_idx = next_input_idx, next_input_idx + 1
        filter_complex.append(
            f"[{music_idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"loudnorm=I={MINIMAL_FAITH_MUSIC_LUFS}:TP=-6,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"afade=t=in:d=0.8,afade=t=out:st={max(duration - 1.2, 0):.2f}:d=1.2[mus]"
        )
        # Duck the music a little more whenever he speaks.
        filter_complex.append("[mus][sc]sidechaincompress=threshold=0.04:ratio=3:attack=30:release=400[duck]")
        mix_inputs = ["[voice]", "[duck]"]
    else:
        filter_complex.append(f"[0:a]{voice_chain}[voice]")
        mix_inputs = ["[voice]"]

    if whoosh_path and os.path.exists(whoosh_path):
        idx, next_input_idx = next_input_idx, next_input_idx + 1
        inputs += ["-i", whoosh_path]
        filter_complex.append(
            f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={MINIMAL_FAITH_WHOOSH_VOLUME},"
            f"atrim=0:{MINIMAL_FAITH_WHOOSH_MAX_SECS},aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[whoosh]"
        )
        mix_inputs.append("[whoosh]")

    if riser_path and os.path.exists(riser_path):
        riser_end = _first_emphasis_offset(segments, start, end, emphasis_words)
        if riser_end is not None and riser_end > 0:
            idx, next_input_idx = next_input_idx, next_input_idx + 1
            riser_duration = min(3.0, riser_end)
            riser_delay_ms = round(max(0.0, riser_end - riser_duration) * 1000)
            inputs += ["-ss", "0", "-t", str(riser_duration), "-i", riser_path]
            filter_complex.append(
                f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={MINIMAL_FAITH_RISER_VOLUME},"
                f"adelay={riser_delay_ms}|{riser_delay_ms},aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[riser]"
            )
            mix_inputs.append("[riser]")

    n = len(mix_inputs)
    filter_complex.append(
        f"{''.join(mix_inputs)}amix=inputs={n}:duration=first:dropout_transition=0,volume={n},alimiter=limit=0.95[a]"
    )

    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", ";".join(filter_complex),
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    _remove(cleanup)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg minimal-faith render failed: {result.stderr[-2000:]}")

    return out_path


def clip_tag(
    filepath: str,
    start: float,
    end: float,
    preset_name: str = DEFAULT_PRESET,
    aspect: str = DEFAULT_ASPECT,
) -> str:
    """The deterministic filename tag burn_captions derives its output paths
    from. Shared here so callers that need to derive a *companion* filename
    for the same clip (cleanup in commentary_render.py) call this instead of
    duplicating the formula and risking drift if it ever changes. The
    default 9:16 keeps the pre-aspect-option names."""
    base = os.path.splitext(os.path.basename(filepath))[0]
    suffix = "" if aspect == DEFAULT_ASPECT else "_" + aspect.replace(":", "x")
    return f"{base}_{start:.1f}-{end:.1f}_{preset_name}{suffix}"


def _crop_chain(
    filepath: str,
    start: float,
    end: float,
    work_dir: str,
    tag: str,
    aspect: str,
    follow_speaker: bool,
    speaker_turns: Optional[list[dict]],
) -> tuple[str, list[str]]:
    ratio = _aspect_ratio(aspect)
    if not follow_speaker:
        return reframe.center_crop_filter(ratio), []
    return reframe.crop_filter(filepath, start, end, work_dir, tag, ratio, speaker_turns)


def _remove(paths: list[str]) -> None:
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def burn_captions(
    filepath: str,
    segments: list[dict],
    start: float,
    end: float,
    emphasis_words: list[str],
    preset_name: str = DEFAULT_PRESET,
    output_dir: str = OUTPUT_DIR,
    pinned_line: Optional[str] = None,
    pinned_line_emphasis: Optional[list[str]] = None,
    comment_bait_question: Optional[str] = None,
    add_captions: bool = True,
    aspect: str = DEFAULT_ASPECT,
    follow_speaker: bool = True,
    speaker_turns: Optional[list[dict]] = None,
) -> str:
    """Trim [start, end) from filepath, burn word-by-word ASS captions, write
    to output_dir. Crops the landscape source to fill the 9:16
    portrait canvas edge-to-edge (no letterbox bars) — the standard
    full-bleed vertical-video look — following the speaker's face.

    pinned_line (with pinned_line_emphasis), when given, adds a second,
    independent static overlay — the clip's single strongest verbatim line,
    pinned at the top of frame for the whole duration, emphasis keywords in
    red — rendered as a second chained `ass=` filter stage so it burns in
    alongside the bottom karaoke captions in the same ffmpeg pass.

    comment_bait_question, when given, adds a third static overlay — a short
    question shown only for the last COMMENT_BAIT_TRAILING_SECONDS, bottom-
    center (distinct from pinned_line's top-center) so the two never
    collide even if both are present.

    add_captions=False skips karaoke captions, pinned_line, and
    comment_bait_question entirely, producing just the reframed clip — for
    callers who'll add their own subtitle template by hand in Resolve
    instead.

    aspect/follow_speaker/speaker_turns: see render_minimal_faith_clip.
    """
    os.makedirs(output_dir, exist_ok=True)
    out_w, out_h = aspect_dims(aspect)

    tag = clip_tag(filepath, start, end, preset_name, aspect)
    out_path = os.path.join(output_dir, f"{tag}_captioned.mp4" if add_captions else f"{tag}.mp4")

    duration = end - start
    # Crop the source to the target aspect (following the speaker when we
    # can find one, else centered), then scale to the delivery resolution
    # before burning captions.
    crop, cleanup = _crop_chain(filepath, start, end, output_dir, tag, aspect, follow_speaker, speaker_turns)
    scale = f"scale={out_w}:{out_h}"

    # comment_bait_question is independent of add_captions — it's a separate
    # editorial layer (see main.py's ClipSelection.comment_bait), not part of
    # the karaoke/pinned-line caption bundle, so it still renders even when
    # add_captions=False (the pipeline's current default everywhere).
    ass_stages = []
    if add_captions:
        ass_path = os.path.join(output_dir, f"{tag}.ass")
        ass_content = generate_ass(segments, start, end, emphasis_words, preset_name, out_h=out_h)
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)
        ass_stages.append(f"ass={reframe.filter_path(ass_path)}")

        if pinned_line:
            pinned_ass_path = os.path.join(output_dir, f"{tag}_pinned.ass")
            pinned_ass_content = generate_static_overlay_ass(pinned_line, pinned_line_emphasis or [], duration, out_h=out_h)
            with open(pinned_ass_path, "w", encoding="utf-8") as f:
                f.write(pinned_ass_content)
            # Chaining a second `ass=` stage burns it in on top of whatever the
            # first stage already rendered — the two files use disjoint screen
            # regions (top-pinned vs. bottom karaoke) so there's no visual
            # conflict, and this avoids merging two style blocks into one file.
            ass_stages.append(f"ass={reframe.filter_path(pinned_ass_path)}")

    if comment_bait_question:
        bait_ass_path = os.path.join(output_dir, f"{tag}_bait.ass")
        bait_start = max(0.0, duration - COMMENT_BAIT_TRAILING_SECONDS)
        bait_ass_content = generate_static_overlay_ass(
            comment_bait_question, [], duration, style=COMMENT_BAIT_STYLE, start=bait_start, out_h=out_h,
        )
        with open(bait_ass_path, "w", encoding="utf-8") as f:
            f.write(bait_ass_content)
        ass_stages.append(f"ass={reframe.filter_path(bait_ass_path)}")

    vf = f"{crop},{scale}," + ",".join(ass_stages) if ass_stages else f"{crop},{scale}"

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start), "-t", str(duration), "-i", filepath,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        out_path,
    ]
    # See commentary_render._run_ffmpeg for why encoding is explicit here —
    # ffmpeg's stderr can echo non-ASCII bytes from the ASS content/path.
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    _remove(cleanup)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg caption burn-in failed: {result.stderr[-2000:]}")

    return out_path


if __name__ == "__main__":
    import sys

    from clippers.pipeline import transcribe

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
