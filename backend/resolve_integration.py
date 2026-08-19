"""
DaVinci Resolve integration (PRD section 5). Resolve must already be open
with a project loaded on this machine — scripting connects to that instance,
it does not launch Resolve.
"""

import os
import platform
import re
import sys
from typing import Optional

_resolve = None
_media_pool_item_cache: dict[str, object] = {}
_timeline_cache: dict[str, object] = {}
_current_project_name: Optional[str] = None

# New timelines are created as 9:16 portrait for Shorts/TikTok/Reels delivery;
# clips are zoomed to fill the frame (crop left/right) rather than letterboxed.
TIMELINE_WIDTH = 1080
TIMELINE_HEIGHT = 1920


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

    os.environ.setdefault("RESOLVE_SCRIPT_API", api_path)
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", lib_path)

    modules_path = os.path.join(api_path, "Modules")
    if modules_path not in sys.path:
        sys.path.append(modules_path)


def _get_resolve():
    global _resolve
    if _resolve is not None:
        return _resolve

    _setup_resolve_env()
    import DaVinciResolveScript as dvr_script

    _resolve = dvr_script.scriptapp("Resolve")
    if _resolve is None:
        raise RuntimeError(
            "Could not connect to DaVinci Resolve. Make sure Resolve is open with a project loaded."
        )
    return _resolve


def _get_project():
    global _current_project_name
    resolve = _get_resolve()
    project_manager = resolve.GetProjectManager()
    project = project_manager.GetCurrentProject()
    if project is None:
        raise RuntimeError("No project open in DaVinci Resolve.")

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

    items = media_pool.ImportMedia([filepath])
    if not items:
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


def _apply_fill_zoom(timeline_item, media_pool_item, timeline) -> float:
    """Scale the clip so it fills the timeline frame instead of being
    letterboxed — equivalent to the center-crop applied in captions.py, but
    for the clean clip living on the Resolve timeline itself. Reads the
    timeline's *actual* current resolution rather than assuming portrait, so
    this is a no-op (zoom 1.0) on a pre-existing landscape timeline whose
    resolution we didn't touch, and correct on any aspect ratio combination.
    Returns the applied zoom (1.0 if skipped, e.g. resolution unavailable)."""
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


def _apply_smart_reframe(timeline_item, zoom: float) -> None:
    """Track the on-screen subject (e.g. a speaker moving on stage) and
    keyframe the clip's position to follow them, instead of leaving it at a
    static fill-crop offset. Only meaningful when a fill-crop is actually
    cropping content away (zoom > 1.0) — a no-op zoom means the whole frame
    is already visible, nothing to track/recenter.

    Best-effort: SmartReframe's resulting keyframes aren't readable via
    GetProperty (same opacity to the scripting API as manually-applied GUI
    templates — confirmed by direct testing), so this can't be verified or
    adjusted after the fact, only triggered. Never raise — this is a bonus
    on top of the static fill-zoom, which remains the fallback framing."""
    if zoom <= 1.0:
        return
    try:
        timeline_item.SmartReframe()
    except Exception:
        pass


def send_clip_to_resolve(filepath: str, title: str, start: float, end: float) -> None:
    project = _get_project()
    media_pool = project.GetMediaPool()

    media_pool_item = _import_media(media_pool, filepath)
    timeline = _get_or_create_timeline(project, media_pool, title)
    project.SetCurrentTimeline(timeline)

    fps_prop = media_pool_item.GetClipProperty("FPS")
    fps = _parse_fps(fps_prop) if fps_prop else float(project.GetSetting("timelineFrameRate"))

    start_frame = round(start * fps)
    end_frame = max(start_frame, round(end * fps) - 1)

    clip_info = {
        "mediaPoolItem": media_pool_item,
        "startFrame": start_frame,
        "endFrame": end_frame,
    }
    result = media_pool.AppendToTimeline([clip_info])
    if not result:
        raise RuntimeError(f"Resolve failed to append clip [{start:.1f}-{end:.1f}] to timeline")

    zoom = _apply_fill_zoom(result[0], media_pool_item, timeline)
    _apply_smart_reframe(result[0], zoom)


if __name__ == "__main__":
    import sys as _sys

    path = _sys.argv[1] if len(_sys.argv) > 1 else "downloads/lgaR7Z9okYM.mp4"
    path = os.path.abspath(path)
    print(f"Connecting to DaVinci Resolve and sending a hardcoded 10s clip from {path} ...")
    send_clip_to_resolve(filepath=path, title="Resolve Integration Test", start=5.0, end=15.0)
    print("Sent successfully — check Resolve for a 'Viral Clips - Resolve Integration Test' timeline.")
