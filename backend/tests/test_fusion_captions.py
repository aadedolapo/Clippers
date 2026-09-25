"""
Tests for backend/fusion_captions.py (retention upgrade plan phase 8, step
5's test harness). The phrase-timing logic is pure Python and always runs;
the comp-generation tests need a live Resolve/Fusion connection and are
skipped (not failed) if one isn't available, matching how the rest of this
codebase treats Resolve as an optional live dependency, not a mock target.

Run: python backend/tests/test_fusion_captions.py
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fusion_captions
import resolve_integration as ri


def _word(text: str, start: float, end: float) -> dict:
    return {"word": text, "start": start, "end": end, "prob": 1.0}


class TestBuildPhrasePlan(unittest.TestCase):
    """Pure logic - no Fusion/Resolve dependency."""

    def test_frame_ranges_and_animation_alternation(self):
        # Four short phrases, well separated (>0.3s gaps) so each word
        # becomes its own phrase boundary-wise once combined into groups
        # by punctuation - constructed simply enough to reason about by
        # hand rather than relying on the full phrase-grouping heuristics.
        words = [
            _word("This", 0.0, 0.3), _word("is", 0.3, 0.5), _word("a", 0.5, 0.6), _word("test.", 0.6, 1.0),
            _word("Another", 2.0, 2.4), _word("phrase", 2.4, 2.8), _word("here.", 2.8, 3.2),
            _word("Third", 4.0, 4.3), _word("one", 4.3, 4.6), _word("now.", 4.6, 5.0),
        ]
        segments = [{"start": 0.0, "end": 5.0, "text": "", "words": words}]
        fps = 24.0

        plan = fusion_captions.build_phrase_plan(segments, 0.0, 5.0, [], fps)

        self.assertGreaterEqual(len(plan), 3, "expected at least 3 phrases from 3 sentences")
        for entry in plan:
            self.assertLess(entry["start_frame"], entry["end_frame"])
            self.assertGreaterEqual(entry["start_frame"], 0)

        # No emphasis words given, so every phrase should alternate between
        # the two non-"up" animations (never "up", since that's reserved
        # for phrases containing an emphasis word).
        anims = [e["anim"] for e in plan]
        self.assertNotIn("up", anims)
        for i in range(len(anims) - 1):
            self.assertNotEqual(anims[i], anims[i + 1], f"consecutive phrases should alternate animation: {anims}")

    def test_emphasis_word_gets_up_animation(self):
        words = [
            _word("The", 0.0, 0.2), _word("secret", 0.2, 0.6), _word("is", 0.6, 0.8), _word("discipline.", 0.8, 1.3),
        ]
        segments = [{"start": 0.0, "end": 1.3, "text": "", "words": words}]
        plan = fusion_captions.build_phrase_plan(segments, 0.0, 1.3, ["discipline"], fps=24.0)

        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["anim"], "up")
        self.assertEqual(plan[0]["highlight_index"], 3)

    def test_empty_range_returns_empty_plan(self):
        segments = [{"start": 0.0, "end": 1.0, "text": "", "words": [_word("hi", 0.0, 0.5)]}]
        plan = fusion_captions.build_phrase_plan(segments, 10.0, 12.0, [], fps=24.0)
        self.assertEqual(plan, [])


def _resolve_reachable() -> bool:
    try:
        ri._get_resolve()
        return True
    except Exception:
        return False


@unittest.skipUnless(_resolve_reachable(), "no live Resolve/Fusion connection available")
class TestCompGeneration(unittest.TestCase):
    """Structural checks against a real generated .comp file: balanced
    braces, expected node count, frame timings - built via a scratch
    fusion.NewComp() the same way fusion_captions.generate_and_attach()
    does, but saved directly rather than attached to a timeline item."""

    def setUp(self):
        resolve = ri._get_resolve()
        self.fusion = resolve.Fusion()
        self.comp = self.fusion.NewComp()
        self.project = resolve.GetProjectManager().GetCurrentProject()
        self.fps = float(self.project.GetSetting("timelineFrameRate"))

    def tearDown(self):
        self.comp.Close()

    def test_comp_saves_with_balanced_braces_and_expected_node_count(self):
        words = [
            _word("This", 0.0, 0.3), _word("is", 0.3, 0.5), _word("a", 0.5, 0.6), _word("test.", 0.6, 1.0),
            _word("Another", 2.0, 2.4), _word("phrase", 2.4, 2.8), _word("here.", 2.8, 3.2),
        ]
        segments = [{"start": 0.0, "end": 3.2, "text": "", "words": words}]

        n_phrases = fusion_captions.build_comp_for_clip(self.comp, segments, 0.0, 3.2, [], self.fps)
        self.assertGreaterEqual(n_phrases, 2)

        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_scratch")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "test_comp.comp")
        self.assertTrue(self.comp.Save(out_path), "comp.Save() should succeed")

        try:
            with open(out_path, encoding="utf-8") as f:
                content = f.read()
            self.assertEqual(content.count("{"), content.count("}"), "braces should balance")
            self.assertEqual(content.count("TextPlus {"), n_phrases, "one TextPlus node per phrase")
            self.assertEqual(content.count("Transform {"), n_phrases, "one Transform node per phrase")
            self.assertEqual(content.count("Blur {"), n_phrases, "one Blur node per phrase")
            self.assertEqual(content.count("Merge {"), n_phrases)
            # MediaIn1/MediaOut1 are required for Resolve to actually
            # composite the clip's own video with the captions (see
            # build_comp_for_clip's docstring) - not optional decoration.
            self.assertEqual(content.count("MediaIn {"), 1)
            self.assertEqual(content.count("MediaOut {"), 1)

            # Frame timings: every keyframe frame number written to the file
            # should fall within the clip's own duration in frames.
            max_frame = round(3.2 * self.fps) + 10  # small slack for the 0.25s tail extension
            frame_numbers = [int(n) for n in re.findall(r"\[(\d+)\] = \{", content)]
            self.assertTrue(frame_numbers, "expected at least one keyframe in the saved comp")
            for f in frame_numbers:
                self.assertLessEqual(f, max_frame, f"keyframe at frame {f} exceeds clip duration")
        finally:
            if os.path.exists(out_path):
                os.remove(out_path)


if __name__ == "__main__":
    unittest.main()
