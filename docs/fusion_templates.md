# Building the Minimal Faith Fusion title templates

One-time manual build (Phase 8, Step 4). Goal: three Fusion title templates — `WM_Up`, `WM_BlurSlideUp`, `WM_SlideRight` — that reproduce the exact animation values already shipping in the Phase 7 ffmpeg/ASS version (`backend/captions.py`'s `_MINIMAL_FAITH_ANIMS`), so Phase 8 can use whichever one code picks per phrase as an *editable* Fusion layer instead of burned-in pixels.

**Project frame rate confirmed live: 24fps** (`Project.GetSetting('timelineFrameRate')` → `24.0`). All frame counts below are ms values from the shipping code, converted to the nearest frame at 24fps (≈41.7ms/frame) — reference the ms figure if you want the exact original timing at a different project frame rate.

## Before you start

- Bundled fonts are already in `backend/assets/fonts/`: `Montserrat-ExtraBold.ttf` (body) and `PlayfairDisplay-Italic-VF.ttf` (emphasis). Install both to your system font library (double-click → Install Font) so Fusion's Text+ can select them — Fusion doesn't read the `fontsdir`-style folder trick ffmpeg uses.
- Colour: emphasis yellow is `#FFD84D`.
- One judgment call from the plan's spec: it says to expose **Text, Highlight word colour, and Size** as macro inputs — three controls. But a single Text+ layer can't easily colour *one word* differently from the rest and still be driven by one plain "Text" string at reuse time. The practical, standard way editors do this in Fusion is **two Text+ layers merged together** (body layer + a second layer for just the emphasis word, positioned to sit inline). That means you'll expose **four** controls in practice: `Text` (the body words), `HighlightText` (the one emphasized word), `HighlightColour`, and `Size`. Noted here rather than silently deviating from the plan without saying so.

## Template 1: `WM_Up`

Used for the phrase that carries the emphasis word (`_MINIMAL_FAITH_ANIMS["up"]` in code) — scale-pop with no movement.

1. **Fusion page** → right-click in the node editor → Add Tool → Title → Text+. Rename it `Body`.
2. Font: **Montserrat ExtraBold**, white, size to taste (this becomes the `Size` macro). Center-anchored (`\an5` equivalent = Fusion's default Text+ anchor, centered).
3. Add a second Text+, rename it `Highlight`. Font: **Playfair Display**, style **Bold Italic** (variable font — use Fusion's weight/italic sliders if the VF exposes them, or the static Bold Italic named instance if your Fusion build lists it separately), colour `#FFD84D`. Position it inline after `Body` (nudge its Center X so the two read as one line — easiest done by eye with both layers visible, since Fusion doesn't auto-flow text between separate Text+ nodes).
4. Merge `Body` (background) and `Highlight` (foreground) with a **Merge** node. This merged output is the template's final output — connect it to the tool's output.
5. On the **Merge** node (or a wrapping Transform if you prefer keyframing there instead), animate **Size** (or the Merge's blend/scale if you keyframe on the Merge node — either works, pick one and be consistent across all three templates):
   - Keyframe at frame 0: Size = **70%**
   - Keyframe at frame **4** (180ms @ 24fps): Size = **100%**
   - Set the spline interpolation between them to **ease-out** (right-click the keyframe → Spline Editor, or use Fusion's "Ease Out" keyframe preset if your version has one under Bezier). If no named ease-out preset exists, set the *outgoing* handle of the 0-frame keyframe flat/fast and the *incoming* handle of the frame-4 keyframe to flatten into it — that produces the cubic ease-out feel (fast start, slow settle) instead of linear.
6. Fade: add a **Fade** modifier (or keyframe overall opacity) — fade in over **2 frames** (100ms), fade out over **1 frame** (60ms) at the clip's end.
7. Expose macro inputs: right-click `Body`'s Styled Text → Add to Modifiers/Expose as Macro → name it `Text`. Same for `Highlight`'s Styled Text → `HighlightText`. Expose `Highlight`'s colour control as `HighlightColour`. Expose the Size keyframe's end value (100%) as `Size` (so callers can scale the whole template up/down without touching keyframes).
8. Save as a title template: right-click the whole group/macro → **Save As Title** (or File → Export as Title, depending on version) → name it exactly `WM_Up`. Save it into Resolve's Titles template folder — on macOS, single-user: `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Templates/Edit/Titles/`. (This is separate from the *Scripts* folder documented in `resolve_api_notes.md` — Titles templates and Scripts use different folders.)

## Template 2: `WM_BlurSlideUp`

Used for alternating plain phrases (`_MINIMAL_FAITH_ANIMS["blur_slide_up"]`). Rise + defocus-to-focus.

1. Same `Body` + `Highlight` Text+ pair as above, merged the same way (or duplicate `WM_Up` and modify — faster).
2. Add a **Blur** tool (Gaussian Blur) in the chain after the Merge.
3. Add a **Transform** tool after the Merge (before or after Blur, doesn't matter visually) to animate vertical position.
4. Keyframes, all starting at frame 0 and ending at frame **5** (200ms @ 24fps):
   - Transform Center Y: start **45px above** final resting position → end at final resting position (i.e. the text drops 45px into place — matches the shipping ASS `\move` from `POS_Y+45` to `POS_Y`).
   - Blur tool's Blur Size: start **10** → end **0**.
   - Ease-out on both, same technique as Template 1.
5. Fade in over **3 frames** (120ms), fade out over **1 frame** (60ms).
6. Expose the same four macros: `Text`, `HighlightText`, `HighlightColour`, `Size`.
7. Save as title template named exactly `WM_BlurSlideUp`.

## Template 3: `WM_SlideRight`

Used for the other alternating slot (`_MINIMAL_FAITH_ANIMS["slide_right"]`). Horizontal slide + light defocus.

1. Same `Body` + `Highlight` pair, merged.
2. Add a **Blur** tool and a **Transform** tool as in Template 2, but animate **Center X** instead of Center Y.
3. Keyframes, frame 0 to frame **5** (220ms @ 24fps — round to 5 frames, same as above; the 20ms difference from Template 2 isn't perceptible at 24fps):
   - Transform Center X: start **35px to the left** of final position → end at final position (matches the shipping ASS `\move` from x=505 to x=540, a 35px shift, not 40px — the plan's summary text says "-30px" but the actual shipped code uses -35px; template should match what's shipped, not the summary).
   - Blur Size: start **4** → end **0.6** (not all the way to 0 — matches the shipping code's `\blur0.6` end state, a deliberate slight residual softness).
   - Ease-out on both.
4. Fade in over **3 frames** (110ms), fade out over **1 frame** (60ms).
5. Expose the same four macros.
6. Save as title template named exactly `WM_SlideRight`.

## Verify before moving to Step 5

- In Resolve, Effects Library → Titles → Templates (or wherever your version lists user title templates) — confirm all three (`WM_Up`, `WM_BlurSlideUp`, `WM_SlideRight`) appear and can be dragged onto a timeline.
- Drop each onto a test timeline, edit its `Text`/`HighlightText`/`HighlightColour` macro fields in the Edit page's Inspector, and confirm the animation plays back correctly and the exposed controls actually drive the visible text/colour.
- Once confirmed, tell me and I'll move on to Step 5 (`backend/fusion_captions.py`, which generates a comp per clip using these exact values as a fallback path, or drives these saved templates via `InsertFusionTitleIntoTimeline` per `resolve_api_notes.md`).
