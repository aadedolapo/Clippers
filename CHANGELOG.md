# Changelog

All notable changes to Clippers are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions
follow [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-24

First public release.

### Added
- Find clip candidates in a long video: faster-whisper transcription, Claude scoring of hook, payoff, standalone sense, lane fit and loopability, plus audio energy.
- Visual rerank: Claude looks at a few frames of each shortlisted clip.
- Clip boundaries snap to camera cuts (PySceneDetect).
- Speaker-following crop for rendered clips (OpenCV YuNet), with optional speaker diarization (pyannote) so the crop follows whoever is talking.
- Send to DaVinci Resolve: a 9:16 timeline with Smart Reframe (Studio) or a static pan (free), plus music bed and whoosh tracks.
- Ready-to-post mp4 for every clip in 9:16, 4:5 or 1:1. It renders even when Resolve can't be reached, for finishing in any editor.
- Clip styles, optional burned-in captions, cold open, spoken outro, B-roll suggestions (Pexels) and beat plans.
- Today's picks across videos, a posted-clip performance log with YouTube Studio CSV import, and a score-vs-retention eval.
- Jobs saved to disk, with Retry and Dismiss for failed analyses.
- Channel description and lanes configurable in `.env`.
- `clippers` command: `init`, `serve`, `doctor`, `report`, `eval`.

[0.1.0]: https://github.com/aadedolapo/Clippers/releases/tag/v0.1.0
