"""
Fusion-based animated captions (retention upgrade plan phase 8, part 2):
generate one Fusion composition per clip so the "Minimal Faith" caption
look (backend/captions.py's minimal-faith preset) lives as an editable
Fusion layer on the Resolve timeline, instead of only baked into an mp4.
"Stamp, don't sculpt" - the three entrance animations are ported as fixed
keyframe recipes from captions.py's _MINIMAL_FAITH_ANIMS, not authored
from scratch per clip.

Fusion scripting notes (see docs/resolve_api_notes.md for the outer
Resolve-level API): the DaVinci Resolve scripting README documents only
attach/detach/import/export for whole Fusion comps as opaque blobs, not
the internal node/tool/parameter object model used here (comp.AddTool,
tool.Input = value, keyframing via comp.BezierSpline()). That inner API
isn't documented anywhere in this Resolve install - every mechanism below
(tool IDs, input names, the keyframe-assignment idiom, node-to-node
connection) was verified empirically against the live running Fusion
instance during Step 5 of the style-test run, not assumed from memory.

STATUS as of the style-test run: video content and portrait framing are
confirmed working end to end (MediaIn1/MediaOut1 wiring, 9:16 fill-zoom,
Smart Reframe - verified against a real screenshot of the rendered
timeline). Caption text is NOT yet reliable - see "Known gap: caption
timing/positioning" below, found via that same screenshot and not yet
resolved. Ship the ffmpeg/ASS minimal-faith path (backend/captions.py) for
production use; treat this module as experimental until that gap is
closed.

Known gaps, found and reported rather than worked around:

- **Caption timing/positioning breaks after ImportFusionComp (unresolved,
  the significant one).** Built and verified correct in isolation (a
  scratch fusion.NewComp() saved and re-read back showed exactly the
  intended per-phrase frame ranges and Center positions), but a screenshot
  of the actual rendered timeline after attaching the comp to a real clip
  showed captions as garbled, overlapping bands in the wrong vertical
  position - i.e. many phrases visible simultaneously instead of one at a
  time. Root-caused as far as time allowed to two compounding causes:
    1. A filter tool's own `Blend` input (tried first, on the Blur tool)
       mixes its processed output against its OWN input, not scene
       opacity - setting it to 0 does not hide the layer at all. Moved the
       fade/hide keyframing to the phrase's own Merge node's `Blend`
       instead (true compositing opacity) - this part is fixed and
       verified correct in the scratch comp.
    2. Fusion's Center coordinate Y-axis is bottom-up (0=bottom, 1=top),
       opposite the ASS/screen convention _MINIMAL_FAITH_ANIMS was ported
       from - fixed by inverting the Y values (see _REST_Y).
    3. **Still unexplained**: re-reading the comp's own tool attributes
       live *after* ImportFusionComp attached it to the real clip showed
       TOOLNT_Region_Start/End values (e.g. {1: -72201.0}) that don't match
       anything set during comp construction (small values like 5, 21, 39
       - the phrase frame numbers). This suggests Resolve rebases the
       comp's frame references against some other absolute frame context
       (possibly the source media's own frame position) when attaching it
       to a real timeline item, in a way that isn't apparent from the
       scripting README or from testing against a detached scratch comp.
       This wasn't reverse-engineered further - flagging it for whoever
       picks this back up rather than guessing again.
- Keyframes created via `tool.Input[frame] = value` always serialize with
  Flags={Linear=true}. No working method to switch a keyframe to Bezier/
  ease-out from script was found (SetKeyFrameFlags exists as an attribute
  on the spline object but is not actually callable - calling it raises
  TypeError). Every animation below is therefore LINEAR, not the
  ease-out-cubic the ffmpeg/ASS minimal-faith path achieves.
- The emphasis word is not visually distinguished (no yellow/serif
  highlight) in this Fusion path yet. TextPlus exposes a per-character
  style-override mechanism (Red1/Green1/Blue1 + Select/SelectElement
  inputs, seen live on a real TextPlus tool), but its exact semantics
  (word index vs. character range) weren't reverse-engineered in the time
  available. The ffmpeg/ASS minimal-faith path already does this
  correctly; this is a Fusion-path-only gap.
"""

import os
import subprocess
import sys
from typing import Optional

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import captions
import resolve_integration as ri

FUSION_CAPTION_TRACK = 2  # video track for the caption comp, per the plan
FUSION_MUSIC_TRACK = 2
FUSION_WHOOSH_TRACK = 3
DEMO_TIMELINE_NAME = "WM Fusion Test"
DEMO_SOURCE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "downloads",
    "The_7-Day_Experiment_That_Changed_the_Lives_of_1_800_People___Dr__Joe_Dispenza_2.mp4",
)
DEMO_CLIP_START = 2379.62
DEMO_WINDOW = (29.5, 53.0)  # same window as _style_test/minimal_faith_test.py

_ANIM_MS = {
    "up": {"duration": 180, "fade_in": 100},
    "blur_slide_up": {"duration": 200, "fade_in": 120},
    "slide_right": {"duration": 220, "fade_in": 110},
}
_FADE_OUT_MS = 60


def _frame(seconds: float, fps: float) -> int:
    return round(seconds * fps)


def _ms_frames(ms: float, fps: float) -> int:
    return max(1, round(ms / 1000.0 * fps))


def build_phrase_plan(
    segments: list[dict], start: float, end: float, emphasis_words: list[str], fps: float
) -> list[dict]:
    """Pure logic, no Fusion dependency: reuse captions.py's phrase
    grouping and return one dict per phrase with its frame range (relative
    to the clip's own start - frame 0 = clip start), which entrance
    animation to use, and the words. Testable without a live Fusion
    connection (see backend/tests/test_fusion_captions.py)."""
    words = captions._segment_words_in_range(segments, start, end)
    if not words:
        return []
    if words[0]["start"] - start < captions.FIRST_CAPTION_DELAY:
        words[0] = {**words[0], "start": start + captions.FIRST_CAPTION_DELAY}
    phrases = captions._group_into_phrases(words, emphasis_words)

    plan = []
    alt = 0
    for i, phrase in enumerate(phrases):
        p_start = max(0.0, phrase[0]["start"] - start)
        natural_end = phrase[-1]["end"] - start + 0.25
        next_start = (phrases[i + 1][0]["start"] - start) if i + 1 < len(phrases) else float("inf")
        p_end = max(p_start, min(natural_end, next_start))

        ei = captions._phrase_emph_index(phrase, emphasis_words)
        anim = "up" if ei is not None else ("blur_slide_up" if alt % 2 == 0 else "slide_right")
        if ei is None:
            alt += 1

        start_frame = _frame(p_start, fps)
        end_frame = max(start_frame + 1, _frame(p_end, fps))
        plan.append({
            "start_frame": start_frame,
            "end_frame": end_frame,
            "anim": anim,
            "text": " ".join(w["word"].strip() for w in phrase),
            "highlight_index": ei,
        })
    return plan


def _keyframe(comp, tool, input_name: str, keyframes: dict) -> None:
    """Assign a keyframed BezierSpline to `input_name` on `tool`. Verified
    live: `tool.X = comp.BezierSpline(); tool.X[frame] = value` is the
    working idiom - a plain dict assignment (`tool.X = {frame: value}`)
    silently does NOT create keyframes (confirmed by re-reading the saved
    .comp file, not just the in-memory getter)."""
    spline = comp.BezierSpline()
    setattr(tool, input_name, spline)
    inp = getattr(tool, input_name)
    for frame, value in keyframes.items():
        inp[frame] = value


# Resting vertical position, converted from the ASS/ffmpeg path's
# POS_Y=1440 out of a 1920-tall frame (75% down from the TOP) into
# Fusion's Center coordinate convention, which is bottom-up (Y=0 at the
# bottom, Y=1 at the top) - confirmed live: using 0.75 directly (assuming
# top-down, like ASS/screen coordinates) put captions near the TOP of
# frame instead of the lower third, and rendered as two overlapping bands
# because of the separate Merge-opacity bug below.
_REST_Y = 1.0 - (captions.MINIMAL_FAITH_POS_Y / captions.PORTRAIT_HEIGHT)
_RISE_Y = 45.0 / captions.PORTRAIT_HEIGHT  # the "blur_slide_up" 45px rise, as a fraction of frame height
_SLIDE_X = 35.0 / captions.PORTRAIT_WIDTH  # the "slide_right" 35px shift, as a fraction of frame width


def _build_phrase_layer(comp, entry: dict, fps: float):
    """One TextPlus -> Transform -> Blur chain for a single phrase's visual
    content, keyframed with the same entrance-animation values as
    captions.py's _MINIMAL_FAITH_ANIMS (linear interpolation only - see
    module docstring). Does NOT set opacity/visibility - that's the calling
    Merge node's job (see build_comp_for_clip), since a filter tool's own
    Blend input mixes its processed output against its OWN input, not
    scene opacity, and setting it to 0 does not hide the layer (confirmed
    live: this was the actual cause of every phrase rendering visible for
    the whole clip simultaneously, not just the ones gated by
    TOOLNT_Region_Start/End)."""
    anim = entry["anim"]
    ms = _ANIM_MS[anim]
    dur_frames = _ms_frames(ms["duration"], fps)
    s0, s1 = entry["start_frame"], entry["end_frame"]

    text_tool = comp.AddTool("TextPlus")
    text_tool.StyledText = entry["text"]
    text_tool.Font = captions.MINIMAL_FAITH_BODY_FONT
    # TextPlus defaults to a 1920x1080 LANDSCAPE canvas regardless of the
    # timeline it ends up on (confirmed live) - without this, its Center/
    # Size values (already 0-1 relative, correct in principle) render
    # against the wrong aspect ratio entirely, which is what made captions
    # land scattered across the frame instead of centered lower-third on
    # the actual 1080x1920 portrait timeline.
    text_tool.Width = captions.PORTRAIT_WIDTH
    text_tool.Height = captions.PORTRAIT_HEIGHT
    text_tool.SetAttrs({"TOOLNT_Region_Start": s0, "TOOLNT_Region_End": s1})

    transform = comp.AddTool("Transform")
    transform.Input = text_tool
    transform.SetAttrs({"TOOLNT_Region_Start": s0, "TOOLNT_Region_End": s1})
    # Static resting position for every anim type (only "up" has no
    # additional Center keyframing below, so this is its only position -
    # previously unset, which left it at Transform's own default (0.5,
    # 0.5) dead center rather than the intended lower-third).
    transform.Center = {1: 0.5, 2: _REST_Y}

    blur = comp.AddTool("Blur")
    blur.Input = transform
    blur.SetAttrs({"TOOLNT_Region_Start": s0, "TOOLNT_Region_End": s1})

    if anim == "up":
        _keyframe(comp, transform, "Size", {s0: 0.7, s0 + dur_frames: 1.0})
    elif anim == "blur_slide_up":
        _keyframe(comp, transform, "Center", {
            s0: {1: 0.5, 2: _REST_Y - _RISE_Y}, s0 + dur_frames: {1: 0.5, 2: _REST_Y},
        })
        _keyframe(comp, blur, "XBlurSize", {s0: 10.0, s0 + dur_frames: 0.0})
        _keyframe(comp, blur, "YBlurSize", {s0: 10.0, s0 + dur_frames: 0.0})
    elif anim == "slide_right":
        _keyframe(comp, transform, "Center", {
            s0: {1: 0.5 - _SLIDE_X, 2: _REST_Y}, s0 + dur_frames: {1: 0.5, 2: _REST_Y},
        })
        _keyframe(comp, blur, "XBlurSize", {s0: 4.0, s0 + dur_frames: 0.6})
        _keyframe(comp, blur, "YBlurSize", {s0: 4.0, s0 + dur_frames: 0.6})

    return blur


def build_comp_for_clip(
    comp, segments: list[dict], start: float, end: float, emphasis_words: list[str], fps: float
) -> int:
    """Populate an empty Fusion comp (from fusion.NewComp()) with one
    entrance-animated phrase layer per phrase, merged over the incoming
    clip's own video (MediaIn1) and wired to MediaOut1. Returns the number
    of phrases built (0 means no words in range - caller should skip
    attaching an empty comp).

    MediaIn1/MediaOut1 are required, not optional: a comp attached to a
    timeline item via ImportFusionComp entirely replaces that clip's node
    graph, and Resolve composites the clip using whatever feeds MediaOut1
    (with MediaIn1 as the clip's own image) - found by inspecting a
    real Resolve-created comp live, since neither name appears anywhere in
    the scripting README. A comp built without them (the first version of
    this function) imports without error but renders nothing: no crash, no
    warning, just a blank clip, which is exactly what happened on the
    first --demo run before this fix."""
    plan = build_phrase_plan(segments, start, end, emphasis_words, fps)
    if not plan:
        return 0

    media_in = comp.AddTool("MediaIn")  # auto-named "MediaIn1"
    composite = media_in

    for entry in plan:
        ms = _ANIM_MS[entry["anim"]]
        fade_in_frames = _ms_frames(ms["fade_in"], fps)
        fade_out_frames = _ms_frames(_FADE_OUT_MS, fps)
        s0, s1 = entry["start_frame"], entry["end_frame"]

        layer = _build_phrase_layer(comp, entry, fps)
        merge = comp.AddTool("Merge")
        merge.Background = composite
        merge.Foreground = layer
        # Real opacity/visibility control lives here, on the Merge that
        # actually composites this phrase over everything under it - a
        # filter's own Blend (tried first, on the Blur tool) only mixes
        # its processed output against its own input, not scene opacity,
        # so it never actually hid anything; every phrase rendered visible
        # for the whole clip until this was moved here (confirmed live).
        # Keyframe curves hold their boundary value outside the defined
        # range, so this is 0 before s0 and after s1 without needing extra
        # keyframes there.
        _keyframe(comp, merge, "Blend", {
            s0: 0.0, s0 + fade_in_frames: 1.0,
            max(s0 + fade_in_frames, s1 - fade_out_frames): 1.0, s1: 0.0,
        })
        composite = merge

    media_out = comp.AddTool("MediaOut")  # auto-named "MediaOut1"
    media_out.Input = composite
    return len(plan)


def generate_and_attach(
    timeline_item, segments: list[dict], start: float, end: float, emphasis_words: list[str],
    output_dir: str, tag: str,
):
    """One-comp-per-clip path: build the comp in a scratch Fusion comp,
    save it to disk, then import it onto `timeline_item` via
    ImportFusionComp. Returns the fusionComp object, or None if there were
    no words in range (nothing to caption)."""
    resolve = ri._get_resolve()
    fusion = resolve.Fusion()
    project = resolve.GetProjectManager().GetCurrentProject()
    fps = float(project.GetSetting("timelineFrameRate"))

    scratch = fusion.NewComp()
    try:
        n_phrases = build_comp_for_clip(scratch, segments, start, end, emphasis_words, fps)
        if n_phrases == 0:
            return None
        os.makedirs(output_dir, exist_ok=True)
        comp_path = os.path.join(output_dir, f"{tag}.comp")
        if not scratch.Save(comp_path):
            raise RuntimeError(f"Fusion comp.Save() failed for {comp_path}")
    finally:
        scratch.Close()

    fusion_comp = timeline_item.ImportFusionComp(comp_path)
    if fusion_comp is None:
        raise RuntimeError(f"TimelineItem.ImportFusionComp({comp_path}) failed")
    return fusion_comp


def insert_phrase_titles_fallback(timeline, plan: list[dict], clip_start_frame: int) -> dict:
    """Fallback per the plan: if comp generation/import isn't viable, insert
    one WM_* Fusion title per phrase at its timecode instead. UNTESTED as
    of this writing - WM_Up/WM_BlurSlideUp/WM_SlideRight don't exist as
    saved title templates yet (docs/fusion_templates.md's manual build
    hasn't been done). Returns {"inserted": n, "failed": n} so callers can
    tell a real failure from "nothing to insert"."""
    name_map = {"up": "WM_Up", "blur_slide_up": "WM_BlurSlideUp", "slide_right": "WM_SlideRight"}
    inserted, failed = 0, 0
    for entry in plan:
        title_name = name_map[entry["anim"]]
        timeline.SetCurrentTimecode(_frames_to_timecode(clip_start_frame + entry["start_frame"]))
        item = timeline.InsertFusionTitleIntoTimeline(title_name)
        if item is None:
            failed += 1
            continue
        inserted += 1
    return {"inserted": inserted, "failed": failed}


def _find_or_import_media(media_pool, filepath: str):
    """resolve_integration._import_media's cache is per-process, so a
    fresh standalone script run (like --demo) doesn't know a file already
    sits in the media pool from an earlier run of this same script.
    Confirmed live while debugging this demo: re-importing a clip that's
    already present AND has since had a Fusion comp attached to it returns
    None (not an error, not an empty list) rather than the existing item -
    resolve_integration._import_media's `if not items or items[0] is None`
    check catches this and raises, which is correct for a genuinely failed
    import but wrong here, where the media is already there. Search the
    media pool by path first rather than assuming import is always safe to
    retry."""
    target = os.path.abspath(filepath)
    for item in media_pool.GetRootFolder().GetClipList():
        prop = item.GetClipProperty("File Path")
        if prop and os.path.abspath(prop) == target:
            ri._media_pool_item_cache[filepath] = item
            return item
    return ri._import_media(media_pool, filepath)


def _frames_to_timecode(frame: int, fps: int = 24) -> str:
    h = frame // (3600 * fps)
    m = (frame // (60 * fps)) % 60
    s = (frame // fps) % 60
    f = frame % fps
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def _run_demo() -> None:
    import tempfile

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from pipeline import transcribe
    import music
    import sfx

    start = DEMO_CLIP_START + DEMO_WINDOW[0]
    end = DEMO_CLIP_START + DEMO_WINDOW[1]
    emphasis_words = ["too sick", "too old", "horrible past", "creator", "profound moment"]

    # Transcribe only the needed window, not the ~52-minute source file:
    # trim it out first (fast, stream copy) and transcribe just that,
    # then offset the resulting word timestamps back to DEMO_SOURCE's own
    # absolute time so they line up with the real start/end used below for
    # both the comp generation and the Resolve timeline append.
    with tempfile.TemporaryDirectory() as tmp_dir:
        trimmed_path = os.path.join(tmp_dir, "demo_window.mp4")
        pad = 1.0
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(max(0.0, start - pad)), "-t", str(end - start + 2 * pad),
             "-i", DEMO_SOURCE, "-c", "copy", trimmed_path],
            check=True, capture_output=True,
        )
        print(f"Transcribing trimmed demo window ({end - start + 2 * pad:.1f}s) instead of the full source...")
        window_segments = transcribe(trimmed_path, model_size="small")

    offset = max(0.0, start - pad)
    segments = [
        {
            **seg,
            "start": seg["start"] + offset,
            "end": seg["end"] + offset,
            "words": [{**w, "start": w["start"] + offset, "end": w["end"] + offset} for w in seg["words"]],
        }
        for seg in window_segments
    ]

    resolve = ri._get_resolve()
    project = resolve.GetProjectManager().GetCurrentProject()
    media_pool = project.GetMediaPool()
    fps = float(project.GetSetting("timelineFrameRate"))

    plan = build_phrase_plan(segments, start, end, emphasis_words, fps)
    print(f"Built phrase plan: {len(plan)} phrases")
    for entry in plan[:5]:
        print(f"  [{entry['start_frame']}-{entry['end_frame']}] {entry['anim']}: {entry['text']!r}")

    timeline = ri._find_existing_timeline(project, DEMO_TIMELINE_NAME)
    if timeline is None:
        timeline = media_pool.CreateEmptyTimeline(DEMO_TIMELINE_NAME)
        # Only force portrait on a timeline we just created (matches
        # resolve_integration._get_or_create_timeline's own convention) -
        # this demo's earlier runs skipped this entirely, leaving the
        # timeline at whatever the project's default (16:9) is.
        timeline.SetSetting("useCustomSettings", "1")
        timeline.SetSetting("timelineResolutionWidth", str(captions.PORTRAIT_WIDTH))
        timeline.SetSetting("timelineResolutionHeight", str(captions.PORTRAIT_HEIGHT))
    project.SetCurrentTimeline(timeline)

    media_pool_item = _find_or_import_media(media_pool, DEMO_SOURCE)
    # startFrame/endFrame are SOURCE MEDIA frame numbers, not timeline
    # frames - they must use the clip's own native fps, not the project's
    # timeline fps (matching resolve_integration.py's established
    # convention). This source file is 29.97fps while this project's
    # timeline is 24fps; using `fps` (24) here silently produced a clip
    # ~20% shorter than intended on an earlier run of this demo.
    fps_prop = media_pool_item.GetClipProperty("FPS")
    src_fps = ri._parse_fps(fps_prop) if fps_prop else fps
    start_frame = round(start * src_fps)
    end_frame = max(start_frame, round(end * src_fps) - 1)
    # _append_clip (not a raw AppendToTimeline dict call) so the 16:9
    # source gets zoomed to fill the 9:16 frame and Smart Reframed, same
    # as every other clip this app sends to Resolve - skipping this step
    # earlier is why the video showed up letterboxed/16:9 instead of
    # filling the portrait frame.
    main_item = ri._append_clip(media_pool, timeline, media_pool_item, start_frame, end_frame, framing="fill")
    print(f"Appended main clip: track 1, timeline frames {main_item.GetStart()}-{main_item.GetEnd()}")

    if timeline.GetTrackCount("video") < FUSION_CAPTION_TRACK:
        timeline.AddTrack("video")

    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "fusion_demo")
    try:
        fusion_comp = generate_and_attach(
            main_item, segments, start, end, emphasis_words, output_dir, tag="wm_fusion_demo",
        )
        print(f"Comp generation + ImportFusionComp: {'OK' if fusion_comp else 'no phrases in range'}")
    except Exception as e:
        print(f"[warn] one-comp-per-clip path failed: {e}")
        print("Falling back to per-phrase title insertion (untested path - see docstring)...")
        fallback_result = insert_phrase_titles_fallback(timeline, plan, main_item.GetStart())
        print(f"Fallback result: {fallback_result}")

    # Music + whoosh on audio tracks 2-3, per the plan. Reuses the main
    # clip's own src_fps for the audio items' frame math too (same
    # approach resolve_integration.py's B-roll insertion uses).
    music_path = music.pick_track("slow")
    whoosh_path = sfx.pick_whoosh()
    for path, track in ((music_path, FUSION_MUSIC_TRACK), (whoosh_path, FUSION_WHOOSH_TRACK)):
        if not path:
            continue
        while timeline.GetTrackCount("audio") < track:
            timeline.AddTrack("audio")
        item = _find_or_import_media(media_pool, path)
        dur_frames = round((end - start) * src_fps) if track == FUSION_MUSIC_TRACK else round(1.5 * src_fps)
        result = media_pool.AppendToTimeline([{
            "mediaPoolItem": item, "startFrame": 0, "endFrame": dur_frames,
            "recordFrame": main_item.GetStart(), "trackIndex": track, "mediaType": 2,
        }])
        ok = bool(result and result[0] is not None)
        print(f"Audio track {track} ({os.path.basename(path)}): {'OK' if ok else 'FAILED'}")

    print(f"\nDone. Check Resolve for timeline '{DEMO_TIMELINE_NAME}'.")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        _run_demo()
    else:
        print("Usage: python fusion_captions.py --demo")
