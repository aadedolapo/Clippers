# DaVinci Resolve scripting API notes (Phase 8, Step 4)

Read from the scripting README bundled with this install, plus live queries against the running instance. Source: `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/README.txt` (last updated 26 May 2026, 1131 lines) and `CHANGELOG.txt`.

## Install confirmed

- **Product: DaVinci Resolve Studio** (not free) — `resolve.GetProductName()` → `"DaVinci Resolve Studio"`.
- **Version: 21.0.2.4** — `resolve.GetVersionString()`.
- A project (`clipper`) is open, confirming the live scripting connection this app already depends on still works.
- Being Studio matters here: `SmartReframe()` (already used by `resolve_integration.py`) and `GenerateSpeech(...)` are both listed under "Studio and AI Scripting APIs" — calls that silently return `False`/an error on free Resolve. Fusion comp/title APIs below are *not* gated this way; they're available on both editions.

## What exists for Fusion/title/comp automation

The DaVinci Resolve scripting README documents a **timeline-level** API for attaching and managing Fusion compositions — it does **not** document Fusion's own internal node/tool/parameter scripting API (see "What's NOT documented" below).

**`Timeline` (insert into timeline):**
- `InsertFusionCompositionIntoTimeline()` → `TimelineItem` — inserts a blank Fusion composition clip.
- `InsertFusionGeneratorIntoTimeline(generatorName)` → `TimelineItem` — inserts a named Fusion generator.
- `InsertFusionTitleIntoTimeline(titleName)` → `TimelineItem` — inserts a named Fusion title. **This is the plan's fallback path** (insert one `WM_*` title per phrase at its timecode).
- `InsertTitleIntoTimeline(titleName)` → `TimelineItem` — inserts a non-Fusion (standard) title, for contrast.
- `CreateFusionClip([timelineItems])` → `TimelineItem` — wraps existing timeline items into a Fusion clip.

**`TimelineItem` (Fusion comps on an existing clip — this is the plan's preferred "one comp per clip" path):**
- `GetFusionCompCount()` → `int`
- `GetFusionCompByIndex(compIndex)` → `fusionComp` (1-based)
- `GetFusionCompNameList()` → `[names...]`
- `GetFusionCompByName(compName)` → `fusionComp`
- `AddFusionComp()` → `fusionComp` — adds a new, empty comp to the clip.
- `ImportFusionComp(path)` → `fusionComp` — **imports a `.comp`/`.setting` file and attaches it to the clip.** This is the actual mechanism for the plan's "generate a Fusion comp as a plain-text Lua table from Python, then attach it."
- `ExportFusionComp(path, compIndex)` → `Bool` — round-trip export, useful for hand-building a comp in the GUI once and exporting it as a template to inspect/copy the Lua table structure.
- `DeleteFusionCompByName(compName)` / `LoadFusionCompByName(compName)` / `RenameFusionCompByName(oldName, newName)`

**Render jobs (for the optional Step 5 render):**
- `Project.AddRenderJob()` → job id string
- `Project.SetRenderSettings({...})`, `Project.SetCurrentRenderFormatAndCodec(format, codec)`
- `Project.StartRendering(jobId, ...)`, `Project.GetRenderJobStatus(jobId)`, `Project.StopRendering()`
- `Project.GetRenderPresetList()` / `LoadRenderPreset(presetName)`

**Checked live**: `proj.GetRenderPresetList()` on this project returns only Resolve's stock presets (`H.264 Master`, `YouTube - 1080p`, `TikTok - 1080p`, etc.) — **there is no "Shorts 1080x1920" preset saved**, despite the plan referencing one by that name. Either save one by hand in Resolve (Deliver page → Render Settings → Save As New Preset), or point the render job at `TikTok - 1080p` (already 1080×1920) instead.

## What's NOT documented here — the actual gap

Everything above is the *outer* API: attach/detach/import/export a whole Fusion comp as a black box. **None of it covers editing what's inside one** — adding a Text+ tool, setting `StyledText`, keyframing a `Transform` or `Blur` node, setting ease curves. That's Fusion's own internal scripting object model (`comp:AddTool("TextPlus")`, `tool.Input:SetValue(...)`, `comp:SetActiveTool()`, `BezierSpline` keyframe objects, etc.), and this README doesn't mention it at all — it's covered by Fusion's own scripting docs (bundled separately with Fusion/Resolve, typically under Fusion's own Help → Scripting, not this file), which we have not read yet.

This confirms the plan's own approach is correct for this Resolve version: rather than trying to drive Fusion's internal object model from Python at runtime (undocumented here, and known to be finicky — keyframe easing in particular may need raw Bezier handle values rather than a named ease preset, since no "ease-out cubic" enum shows up anywhere in this API), **build the three animation templates by hand in the Fusion UI once** (Step 4's manual step, see `fusion_templates.md`), then either:
1. **Export one as a `.comp` file** (`TimelineItem.ExportFusionComp`) to see the actual Lua table structure a hand-built comp serializes to, and pattern-match generated per-clip comps against it, or
2. Use `InsertFusionTitleIntoTimeline` to insert the saved title template per phrase and only touch its exposed macro inputs (Text, Highlight word colour, Size — the plan's explicit ask for what to expose), never its internal node graph.

## Known version-specific risk

The Resolve scripting changelog shows meaningful API churn version-to-version (e.g. `useSpeakerDetection` added in 21.0 Beta, various Studio/AI methods added recently) — the plan's own warning that "Fusion methods and their arguments differ across Resolve 18/19/20" is consistent with what's in this changelog. Everything recorded above is specific to the installed **21.0.2.4**; re-check this file after any Resolve update before trusting these method names again.
