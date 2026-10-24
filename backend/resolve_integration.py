"""
DaVinci Resolve integration (PRD section 5). Resolve must already be open
with a project loaded on this machine — scripting connects to that instance,
it does not launch Resolve.
"""

import logging
import os
import platform
import re
import sys
from typing import Callable, Optional

import captions
import reframe

logger = logging.getLogger(__name__)

_resolve = None
_media_pool_item_cache: dict[str, object] = {}
_timeline_cache: dict[str, object] = {}
_current_project_name: Optional[str] = None

# New timelines are created as 9:16 portrait for Shorts/TikTok/Reels delivery;
# clips are zoomed to fill the frame (crop left/right) rather than letterboxed.
TIMELINE_WIDTH = 1080
TIMELINE_HEIGHT = 1920

# How long a B-roll cutaway plays before cutting back to the main clip, when
# the pick itself doesn't specify a duration (or the source footage is
# shorter than that). PRD §12's example targets "2-4 B-roll changes" across
# a short clip, which this default is sized for.
DEFAULT_BROLL_CUTAWAY_SECONDS = 2.5
BROLL_TRACK_INDEX = 2

# Audio tracks for the minimal-faith music bed / whoosh, when sent to the
# Resolve timeline alongside the clip (separate namespace from
# BROLL_TRACK_INDEX above, which is a video track) — track 1 stays the main
# clip's own synced audio.
MUSIC_TRACK_INDEX = 2
WHOOSH_TRACK_INDEX = 3


class ResolveUnavailable(RuntimeError):
    """Resolve isn't reachable at all (not installed, not running, external
    scripting off, or no project open), as opposed to one clip failing.
    Callers use it to skip Resolve and still render the mp4."""


def _setup_resolve_env() -> None:
    system = platform.system()
    if system == "Darwin":
        api_path = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
        lib_path = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
    elif system == "Windows":
        api_path = os.path.expandvars(
            r"%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"
        )
        lib_path = r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"
    else:  # Linux
        api_path = "/opt/resolve/Developer/Scripting"
        lib_path = "/opt/resolve/libs/Fusion/fusionscript.so"

    os.environ["RESOLVE_SCRIPT_API"] = api_path
    os.environ["RESOLVE_SCRIPT_LIB"] = lib_path

    modules_path = os.path.join(api_path, "Modules")
    if modules_path not in sys.path:
        sys.path.insert(0, modules_path)


# def _get_resolve():
#     global _resolve
#     if _resolve is not None:
#         return _resolve

#     _setup_resolve_env()
#     import DaVinciResolveScript as dvr_script

#     resolve = dvr_script.scriptapp("Resolve", "192.168.1.178")
#     # scriptapp("Resolve") can return a non-None object whose methods are all
#     # None instead of failing outright — happens when Resolve's "External
#     # scripting using" preference is off, which otherwise surfaces later as
#     # an opaque "'NoneType' object is not callable" on the first method call.
#     if resolve is None or not callable(getattr(resolve, "GetProjectManager", None)):
#         raise RuntimeError(
#             "Could not connect to DaVinci Resolve. Make sure Resolve is open with a "
#             "project loaded, and External Scripting is enabled in Resolve > "
#             "Preferences > System > General ('External scripting using' = 'Local')."
#         )
#     _resolve = resolve
#     return _resolve
def _get_resolve():
    global _resolve

    if _resolve is not None:
        return _resolve

    _setup_resolve_env()

    try:
        import DaVinciResolveScript as dvr_script
    except ImportError as exc:
        raise ResolveUnavailable(
            "Could not import DaVinciResolveScript. "
            "Check the Resolve scripting installation."
        ) from exc

    # First try the normal local connection.
    resolve = dvr_script.scriptapp("Resolve")

    # On some macOS/Resolve 21 installations, the default local
    # connection returns None even though Resolve is discoverable.
    if resolve is None:
        try:
            hosts = dvr_script.pinghosts("")
        except Exception:
            hosts = {}

        for host_info in hosts.values():
            if host_info.get("Hosts", {}).get(1.0) == "Resolve":
                ip = host_info.get("IP")

                if ip:
                    try:
                        resolve = dvr_script.scriptapp("Resolve", ip)
                    except Exception:
                        resolve = None

                if resolve is not None:
                    break

    if resolve is None:
        raise ResolveUnavailable(
            "Could not connect to DaVinci Resolve. "
            "Resolve scripting is enabled, but no reachable Resolve "
            "instance was found."
        )

    if not callable(getattr(resolve, "GetProjectManager", None)):
        raise ResolveUnavailable(
            "Connected to Resolve, but the scripting API is invalid."
        )

    _resolve = resolve
    return _resolve

def _get_project():
    global _current_project_name, _resolve
    project_manager = _get_resolve().GetProjectManager()
    if project_manager is None:
        # The cached connection went stale (Resolve was closed or restarted
        # since the last send): reconnect once.
        _resolve = None
        project_manager = _get_resolve().GetProjectManager()
        if project_manager is None:
            raise ResolveUnavailable("Lost the connection to DaVinci Resolve.")
    project = project_manager.GetCurrentProject()
    if project is None:
        raise ResolveUnavailable("No project open in DaVinci Resolve.")

    # Media pool items and timeline objects are tied to the project they came
    # from; if the user switches the active project in Resolve's GUI between
    # calls, cached objects from the old project silently fail Resolve API
    # calls (e.g. AppendToTimeline) against the new one. Detect the switch and
    # drop the caches rather than reuse stale cross-project references.
    name = project.GetName()
    if name != _current_project_name:
        _media_pool_item_cache.clear()
        _timeline_cache.clear()
        _current_project_name = name

    return project


def _import_media(media_pool, filepath: str):
    if filepath in _media_pool_item_cache:
        return _media_pool_item_cache[filepath]

    # ImportMedia can return a non-empty list containing None for a failed
    # import (confirmed on AppendToTimeline in earlier testing) rather than
    # an empty list — `if not items` alone misses that case and a bare None
    # would get cached and reused forever, silently poisoning every later
    # call for this filepath.
    items = media_pool.ImportMedia([filepath])
    if not items or items[0] is None:
        raise RuntimeError(f"DaVinci Resolve failed to import {filepath}")

    item = items[0]
    _media_pool_item_cache[filepath] = item
    return item


def _find_existing_timeline(project, timeline_name: str):
    for i in range(1, project.GetTimelineCount() + 1):
        timeline = project.GetTimelineByIndex(i)
        if timeline and timeline.GetName() == timeline_name:
            return timeline
    return None


def _get_or_create_timeline(project, media_pool, title: str):
    timeline_name = f"Viral Clips - {title}"
    if timeline_name in _timeline_cache:
        return _timeline_cache[timeline_name]

    timeline = _find_existing_timeline(project, timeline_name)
    if timeline is None:
        timeline = media_pool.CreateEmptyTimeline(timeline_name)
        if timeline is None:
            raise RuntimeError(f"Failed to create Resolve timeline '{timeline_name}'")
        # Only force portrait on timelines we just created — don't clobber the
        # resolution of a pre-existing timeline someone else set up.
        timeline.SetSetting("useCustomSettings", "1")
        timeline.SetSetting("timelineResolutionWidth", str(TIMELINE_WIDTH))
        timeline.SetSetting("timelineResolutionHeight", str(TIMELINE_HEIGHT))

    _timeline_cache[timeline_name] = timeline
    return timeline


def _parse_fps(fps_value) -> float:
    match = re.match(r"[\d.]+", str(fps_value))
    if not match:
        raise RuntimeError(f"Could not parse frame rate from Resolve clip property: {fps_value!r}")
    return float(match.group())


def _parse_resolution(res_value) -> tuple[float, float]:
    match = re.match(r"(\d+)x(\d+)", str(res_value))
    if not match:
        raise RuntimeError(f"Could not parse resolution from Resolve clip property: {res_value!r}")
    return float(match.group(1)), float(match.group(2))


def _apply_fill_zoom(timeline_item, media_pool_item, timeline, framing: str = "fill") -> float:
    """Set the clip's framing on the timeline.

    framing="fill" (default): scale the clip to fill the timeline frame
    (crops overflow, no letterboxing) — matches captions.py's burn_captions,
    which center-crops to fill the 9:16 canvas edge-to-edge. Reads the
    timeline's *actual* current resolution rather than assuming portrait, so
    this is a no-op (zoom 1.0) on a pre-existing landscape timeline whose
    resolution we didn't touch, and correct on any aspect ratio combination.
    framing="fit": leave Resolve's own default zoom (1.0) as-is — the whole
    clip visible, letterboxed on one axis where aspect ratios differ. Kept
    for callers that want that look instead.

    Returns the applied zoom (1.0 for "fit", or whenever resolution info is
    unavailable to compute "fill")."""
    if framing == "fit":
        return 1.0

    res_prop = media_pool_item.GetClipProperty("Resolution")
    if not res_prop:
        return 1.0
    src_w, src_h = _parse_resolution(res_prop)
    tl_w = float(timeline.GetSetting("timelineResolutionWidth"))
    tl_h = float(timeline.GetSetting("timelineResolutionHeight"))

    # Resolve's default zoom=1.0 is a "fit" scale (whole clip visible,
    # letterboxed on one axis) — the smaller of the two axis ratios. "Fill"
    # (crop overflow, no letterboxing) is the larger of the two. The extra
    # zoom needed is fill/fit; this is 1.0 (no-op) whenever the clip and
    # timeline already share an aspect ratio.
    fit_scale = min(tl_w / src_w, tl_h / src_h)
    fill_scale = max(tl_w / src_w, tl_h / src_h)
    zoom = fill_scale / fit_scale
    timeline_item.SetProperty("ZoomX", zoom)
    timeline_item.SetProperty("ZoomY", zoom)
    return zoom


def _apply_smart_reframe(timeline_item, zoom: float) -> bool:
    """Track the on-screen subject (e.g. a speaker moving on stage) and
    keyframe the clip's position to follow them, instead of leaving it at a
    static fill-crop offset. Only meaningful when a fill-crop is actually
    cropping content away (zoom > 1.0) — a no-op zoom means the whole frame
    is already visible, nothing to track/recenter.

    Best-effort: SmartReframe's resulting keyframes aren't readable via
    GetProperty (same opacity to the scripting API as manually-applied GUI
    templates — confirmed by direct testing), so this can't be verified or
    adjusted after the fact, only triggered. Never raise — this is a bonus
    on top of the static fill-zoom, which remains the fallback framing.

    Returns whether Resolve reported success (free Resolve doesn't have
    Smart Reframe, so this is False there)."""
    if zoom <= 1.0:
        return False
    try:
        return bool(timeline_item.SmartReframe())
    except Exception:
        return False


def _apply_static_pan(timeline_item, timeline, zoom: float, face: Optional[tuple]) -> None:
    """Free-Resolve stand-in for Smart Reframe: pan the fill-cropped clip so
    the speaker's median position (reframe.median_face_offset) sits in the
    middle of the frame. Static, because the scripting API can set a
    property but not keyframe it, so it can't follow movement the way the
    rendered mp4's crop does. Clamped so the frame edge never shows."""
    if zoom <= 1.0 or face is None:
        return
    median_cx, src_w, src_h = face
    tl_w = float(timeline.GetSetting("timelineResolutionWidth"))
    tl_h = float(timeline.GetSetting("timelineResolutionHeight"))
    scale = max(tl_w / src_w, tl_h / src_h)
    max_pan = max(0.0, (src_w * scale - tl_w) / 2)
    pan = -(median_cx - src_w / 2) * scale
    pan = min(max(pan, -max_pan), max_pan)
    try:
        timeline_item.SetProperty("Pan", pan)
    except Exception:
        logger.exception("setting static Pan failed")


def _append_clip(
    media_pool,
    timeline,
    media_pool_item,
    start_frame: Optional[int] = None,
    end_frame: Optional[int] = None,
    framing: str = "fill",
    face_hint: Optional[Callable[[], Optional[tuple]]] = None,
) -> None:
    """Append one item to the current timeline — the current end of the
    timeline, since Resolve's AppendToTimeline always adds after whatever
    was appended last, which is what gives repeated calls (intro, then
    sermon range, then close) their left-to-right order for free.

    Per Resolve's own scripting README, the clipInfo-dict form of
    AppendToTimeline documents "startFrame"/"endFrame" without marking them
    optional (unlike mediaType/trackIndex/recordFrame, which are marked
    optional) — so for a full-length append (pre-rendered commentary
    segments that don't need trimming), use the plain MediaPoolItem form
    instead of a dict missing those keys.

    Returns the appended timeline item, so callers can read back where it
    actually landed on the timeline (GetStart()/GetEnd()) — a new timeline's
    first frame is Resolve's own start timecode (often 01:00:00:00, i.e.
    frame 86400 at 24fps), not frame 0, and an existing timeline may already
    have other content, so "where this clip landed" is never safely
    assumed from the source-media start_frame/end_frame passed in here."""
    if start_frame is not None and end_frame is not None:
        result = media_pool.AppendToTimeline([{
            "mediaPoolItem": media_pool_item,
            "startFrame": start_frame,
            "endFrame": end_frame,
        }])
    else:
        result = media_pool.AppendToTimeline([media_pool_item])
    # AppendToTimeline can return a non-empty list containing None for a
    # failed append rather than an empty list — `if not result` alone misses
    # that case, leaving result[0] as None and causing an opaque
    # "'NoneType' object is not callable" a few lines later when it's used
    # as if it were a real timeline item.
    if not result or result[0] is None:
        raise RuntimeError("Resolve failed to append a clip to the timeline")

    zoom = _apply_fill_zoom(result[0], media_pool_item, timeline, framing=framing)
    if not _apply_smart_reframe(result[0], zoom) and face_hint is not None:
        _apply_static_pan(result[0], timeline, zoom, face_hint())
    return result[0]


def _face_hint(filepath: str, start: float, end: float) -> Callable[[], Optional[tuple]]:
    """Deferred, so face detection only runs when Smart Reframe failed."""
    return lambda: reframe.median_face_offset(filepath, start, end)


def _insert_broll_overlay(
    media_pool,
    broll_item,
    start_frame: int,
    end_frame: int,
    record_frame: int,
    track_index: int,
) -> None:
    """Drop a B-roll item onto `track_index` at an explicit timeline
    position (`record_frame`) rather than appending after the track's last
    clip — AppendToTimeline's clipInfo-dict form accepts recordFrame/
    trackIndex/mediaType as documented-optional keys for exactly this.
    mediaType=1 (video only) leaves the sermon clip's own audio on track 1
    as the only audio, instead of layering the stock footage's ambient
    sound over the narration."""
    result = media_pool.AppendToTimeline([{
        "mediaPoolItem": broll_item,
        "startFrame": start_frame,
        "endFrame": end_frame,
        "recordFrame": record_frame,
        "trackIndex": track_index,
        "mediaType": 1,
    }])
    if not result or result[0] is None:
        raise RuntimeError("Resolve failed to insert a B-roll overlay clip")


def _insert_audio_overlay(
    media_pool,
    audio_item,
    start_frame: int,
    end_frame: int,
    record_frame: int,
    track_index: int,
) -> None:
    """Drop an audio-only item onto `track_index` at an explicit timeline
    position — same AppendToTimeline dict pattern as _insert_broll_overlay's
    video track, just mediaType=2 (audio only) instead of 1."""
    result = media_pool.AppendToTimeline([{
        "mediaPoolItem": audio_item,
        "startFrame": start_frame,
        "endFrame": end_frame,
        "recordFrame": record_frame,
        "trackIndex": track_index,
        "mediaType": 2,
    }])
    if not result or result[0] is None:
        raise RuntimeError("Resolve failed to insert an audio overlay clip")


def _ensure_audio_tracks(timeline, count: int) -> None:
    """Add audio tracks until there are at least `count`. With the music bed
    off, the whoosh still goes on track 3, so track 2 has to exist too."""
    while timeline.GetTrackCount("audio") < count:
        if not timeline.AddTrack("audio"):
            raise RuntimeError("Resolve failed to add an audio track")


def _add_music_and_whoosh(
    media_pool,
    timeline,
    main_item,
    fps: float,
    duration: float,
    music_path: Optional[str],
    whoosh_path: Optional[str],
) -> None:
    """Add the minimal-faith music bed / whoosh as separate audio tracks
    (2 and 3) under `main_item`'s landing position on the timeline — same
    trim offsets as captions.py's rendered mp4 (MINIMAL_FAITH_MUSIC_OFFSET/
    MINIMAL_FAITH_WHOOSH_MAX_SECS) so what plays on the timeline roughly
    matches the deliverable file. Not ducked or level-matched here — that
    mixing only happens in the rendered mp4's ffmpeg pass; this is a plain
    unmixed overlay for editing in Resolve directly.

    Best-effort per track, matching _insert_broll_overlay's tolerance for a
    bad pick: a failed music or whoosh insert is logged and skipped rather
    than failing the whole send."""
    if not music_path and not whoosh_path:
        return
    record_frame = main_item.GetStart()

    if music_path and os.path.exists(music_path):
        try:
            _ensure_audio_tracks(timeline, MUSIC_TRACK_INDEX)
            music_item = _import_media(media_pool, music_path)
            start_frame = round(captions.MINIMAL_FAITH_MUSIC_OFFSET * fps)
            end_frame = start_frame + max(0, round(duration * fps) - 1)
            _insert_audio_overlay(media_pool, music_item, start_frame, end_frame, record_frame, MUSIC_TRACK_INDEX)
        except Exception:
            logger.exception(f"failed to insert music bed from {music_path}")

    if whoosh_path and os.path.exists(whoosh_path):
        try:
            _ensure_audio_tracks(timeline, WHOOSH_TRACK_INDEX)
            whoosh_item = _import_media(media_pool, whoosh_path)
            end_frame = max(0, round(captions.MINIMAL_FAITH_WHOOSH_MAX_SECS * fps) - 1)
            _insert_audio_overlay(media_pool, whoosh_item, 0, end_frame, record_frame, WHOOSH_TRACK_INDEX)
        except Exception:
            logger.exception(f"failed to insert whoosh from {whoosh_path}")


def send_clip_to_resolve(
    filepath: str,
    title: str,
    start: float,
    end: float,
    framing: str = "fill",
    cold_open: Optional[dict] = None,
    music_path: Optional[str] = None,
    whoosh_path: Optional[str] = None,
) -> None:
    """cold_open, when given ({"start", "end"} in source-media seconds), is
    appended from the same media pool item immediately before the main
    range — same flash-forward effect as the standalone captioned mp4
    (commentary_render.build_clip_with_cold_open), but as two plain
    timeline appends rather than a rendered crossfade, matching how the
    commentary bookends are appended as separate items.

    music_path/whoosh_path, when given, land as separate audio tracks under
    the main clip — see _add_music_and_whoosh."""
    project = _get_project()
    media_pool = project.GetMediaPool()

    media_pool_item = _import_media(media_pool, filepath)
    timeline = _get_or_create_timeline(project, media_pool, title)
    project.SetCurrentTimeline(timeline)

    fps_prop = media_pool_item.GetClipProperty("FPS")
    fps = _parse_fps(fps_prop) if fps_prop else float(project.GetSetting("timelineFrameRate"))

    if cold_open:
        co_start_frame = round(cold_open["start"] * fps)
        co_end_frame = max(co_start_frame, round(cold_open["end"] * fps) - 1)
        _append_clip(media_pool, timeline, media_pool_item, co_start_frame, co_end_frame, framing=framing,
                     face_hint=_face_hint(filepath, cold_open["start"], cold_open["end"]))

    start_frame = round(start * fps)
    end_frame = max(start_frame, round(end * fps) - 1)

    main_item = _append_clip(media_pool, timeline, media_pool_item, start_frame, end_frame, framing=framing,
                             face_hint=_face_hint(filepath, start, end))
    _add_music_and_whoosh(media_pool, timeline, main_item, fps, end - start, music_path, whoosh_path)


def send_clip_with_broll_to_resolve(
    filepath: str,
    title: str,
    start: float,
    end: float,
    broll_clips: list[dict],
    framing: str = "fill",
    cold_open: Optional[dict] = None,
    music_path: Optional[str] = None,
    whoosh_path: Optional[str] = None,
) -> None:
    """Same as send_clip_to_resolve, but also drops each already-downloaded
    B-roll pick onto a second video track as a full-frame cutaway (video
    only — see _insert_broll_overlay — so the sermon clip's audio keeps
    playing underneath). Each entry in `broll_clips` is
    {"filepath": local path, "offset": seconds from the sermon clip's own
    start (None falls back to the clip's very start), "duration": optional
    seconds, capped to DEFAULT_BROLL_CUTAWAY_SECONDS and to whatever's left
    in the clip}. Offsets come from broll.locate_quote_offset matching each
    pick's source quote against the transcript, so cutaways land near the
    part of the script they illustrate rather than all piling up at frame 0.

    cold_open, when given, is appended before the main clip exactly as in
    send_clip_to_resolve — B-roll offsets/record-frame math below is
    unaffected since it's still anchored to main_item's own GetStart().

    music_path/whoosh_path, when given, land as separate audio tracks under
    the main clip — see _add_music_and_whoosh.

    Best-effort per pick: a bad download/import for one B-roll clip is
    logged and skipped rather than failing the whole send, since the main
    clip having landed in Resolve matters more than any single cutaway."""
    project = _get_project()
    media_pool = project.GetMediaPool()

    media_pool_item = _import_media(media_pool, filepath)
    timeline = _get_or_create_timeline(project, media_pool, title)
    project.SetCurrentTimeline(timeline)

    fps_prop = media_pool_item.GetClipProperty("FPS")
    fps = _parse_fps(fps_prop) if fps_prop else float(project.GetSetting("timelineFrameRate"))

    if cold_open:
        co_start_frame = round(cold_open["start"] * fps)
        co_end_frame = max(co_start_frame, round(cold_open["end"] * fps) - 1)
        _append_clip(media_pool, timeline, media_pool_item, co_start_frame, co_end_frame, framing=framing,
                     face_hint=_face_hint(filepath, cold_open["start"], cold_open["end"]))

    clip_start_frame = round(start * fps)
    clip_end_frame = max(clip_start_frame, round(end * fps) - 1)
    main_item = _append_clip(media_pool, timeline, media_pool_item, clip_start_frame, clip_end_frame, framing=framing,
                             face_hint=_face_hint(filepath, start, end))
    _add_music_and_whoosh(media_pool, timeline, main_item, fps, end - start, music_path, whoosh_path)

    if not broll_clips:
        return

    if timeline.GetTrackCount("video") < BROLL_TRACK_INDEX:
        timeline.AddTrack("video")

    # B-roll offsets are seconds into the *clip*, but AppendToTimeline's
    # recordFrame is an absolute *timeline* frame — the main clip's actual
    # landing position (main_item.GetStart()), not clip_start_frame (which
    # is a source-media in-point, not a timeline position, and would be
    # off by however many frames the timeline's own start timecode is from
    # zero — e.g. a whole 86400-frame/1-hour offset on a fresh timeline —
    # silently stranding every B-roll cutaway far from the clip it belongs
    # on rather than overlaid on it).
    main_timeline_start = main_item.GetStart()

    clip_duration = end - start
    for pick in broll_clips:
        try:
            offset = pick.get("offset")
            offset = 0.0 if offset is None else max(0.0, min(float(offset), clip_duration - 0.1))

            broll_item = _import_media(media_pool, pick["filepath"])
            broll_fps_prop = broll_item.GetClipProperty("FPS")
            broll_fps = _parse_fps(broll_fps_prop) if broll_fps_prop else fps

            cutaway_seconds = min(
                float(pick.get("duration") or DEFAULT_BROLL_CUTAWAY_SECONDS),
                clip_duration - offset,
            )
            if cutaway_seconds <= 0:
                continue

            broll_end_frame = max(0, round(cutaway_seconds * broll_fps) - 1)
            record_frame = main_timeline_start + round(offset * fps)

            _insert_broll_overlay(media_pool, broll_item, 0, broll_end_frame, record_frame, BROLL_TRACK_INDEX)
        except Exception:
            logger.exception(f"failed to insert B-roll overlay from {pick.get('filepath')}")
            continue


def send_clip_with_commentary_to_resolve(
    filepath: str,
    title: str,
    start: float,
    end: float,
    intro_path: Optional[str] = None,
    close_path: Optional[str] = None,
    framing: str = "fill",
    cold_open: Optional[dict] = None,
    music_path: Optional[str] = None,
    whoosh_path: Optional[str] = None,
) -> None:
    """Same as send_clip_to_resolve, but appends the already-rendered
    commentary segments (background + captions + narration baked in by
    commentary_render.py) as their own timeline items immediately before/
    after the sermon clip range — one continuous sequence on the same
    timeline, in the order cold_open -> intro -> sermon -> close. cold_open
    is a sub-range of the same source file (like send_clip_to_resolve's),
    not a separately rendered segment, so it's appended by frame math
    before intro_path rather than via _import_media on a rendered file.

    framing applies to all items ("fit" leaves Resolve's default zoom
    alone, matching the letterboxed look burn_captions now produces by
    default; "fill" crops to fill as before). It's mostly moot for the
    commentary segments themselves since those are already rendered at the
    timeline's own portrait resolution — passed through for consistency."""
    project = _get_project()
    media_pool = project.GetMediaPool()
    timeline = _get_or_create_timeline(project, media_pool, title)
    project.SetCurrentTimeline(timeline)

    media_pool_item = _import_media(media_pool, filepath)
    fps_prop = media_pool_item.GetClipProperty("FPS")
    fps = _parse_fps(fps_prop) if fps_prop else float(project.GetSetting("timelineFrameRate"))

    if cold_open:
        co_start_frame = round(cold_open["start"] * fps)
        co_end_frame = max(co_start_frame, round(cold_open["end"] * fps) - 1)
        _append_clip(media_pool, timeline, media_pool_item, co_start_frame, co_end_frame, framing=framing,
                     face_hint=_face_hint(filepath, cold_open["start"], cold_open["end"]))

    if intro_path:
        intro_item = _import_media(media_pool, intro_path)
        _append_clip(media_pool, timeline, intro_item, framing=framing)

    start_frame = round(start * fps)
    end_frame = max(start_frame, round(end * fps) - 1)
    main_item = _append_clip(media_pool, timeline, media_pool_item, start_frame, end_frame, framing=framing,
                             face_hint=_face_hint(filepath, start, end))
    _add_music_and_whoosh(media_pool, timeline, main_item, fps, end - start, music_path, whoosh_path)

    if close_path:
        close_item = _import_media(media_pool, close_path)
        _append_clip(media_pool, timeline, close_item, framing=framing)


if __name__ == "__main__":
    import sys as _sys

    path = _sys.argv[1] if len(_sys.argv) > 1 else "downloads/lgaR7Z9okYM.mp4"
    path = os.path.abspath(path)
    print(f"Connecting to DaVinci Resolve and sending a hardcoded 10s clip from {path} ...")
    send_clip_to_resolve(filepath=path, title="Resolve Integration Test", start=5.0, end=15.0)
    print("Sent successfully — check Resolve for a 'Viral Clips - Resolve Integration Test' timeline.")
