"""
Step 1 of the build order: download a YouTube video + audio with yt-dlp,
transcribe it with faster-whisper, and print word-level timestamps.

Run directly for a console-only smoke test:
    python pipeline.py "<youtube url>"
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from typing import Optional

import yt_dlp
from faster_whisper import WhisperModel

DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "small")


COOKIES_FROM_BROWSER = os.environ.get("YT_COOKIES_BROWSER", "chrome")


def _is_video_track_static(filepath: str, sample_count: int = 4) -> bool:
    """Detect the SABR-fallback failure mode where yt-dlp downloads a real audio
    track but a frozen/blank placeholder video track (same frame repeated for the
    whole duration). Samples a few timestamps and checks if the decoded frames are
    all identical."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", filepath],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        duration = float(probe.stdout.strip())
    except ValueError:
        return False

    hashes = set()
    for i in range(sample_count):
        ts = duration * (i + 1) / (sample_count + 1)
        frame = subprocess.run(
            ["ffmpeg", "-ss", str(ts), "-i", filepath, "-frames:v", "1",
             "-f", "image2pipe", "-vcodec", "png", "-loglevel", "error", "pipe:1"],
            capture_output=True,
        )
        hashes.add(hashlib.sha256(frame.stdout).hexdigest())
    return len(hashes) == 1


def download_video(url: str) -> dict:
    """Download best video+audio as a single mp4 and return yt-dlp's info dict.

    YouTube's 2025 SABR streaming restrictions can make yt-dlp's default web
    client fall back to a broken video stream (real audio, frozen/blank video)
    for some formats. Try the browser-cookie path first for quality, but detect
    that failure mode and retry with the android client, which reliably gets a
    real (if lower-res) progressive stream without needing cookies or a PO token.
    """
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    outtmpl = os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s")

    attempts = [
        {
            "format": (
                "bestvideo[ext=mp4][protocol^=https]+bestaudio[ext=m4a][protocol^=https]"
                "/best[ext=mp4][protocol^=https]"
                "/bestvideo[ext=mp4]+bestaudio[ext=m4a]"
                "/best[ext=mp4]/best"
            ),
            "cookiesfrombrowser": (COOKIES_FROM_BROWSER,),
        },
        {
            "format": "best[ext=mp4]/best",
            "extractor_args": {"youtube": {"player_client": ["android"]}},
        },
    ]

    info = None
    filepath = None
    last_error = None
    for attempt_opts in attempts:
        ydl_opts = {
            "outtmpl": outtmpl,
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": False,
            **attempt_opts,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = ydl.prepare_filename(info)
                if not os.path.exists(filepath):
                    base, _ = os.path.splitext(filepath)
                    filepath = base + ".mp4"
        except yt_dlp.utils.DownloadError as e:
            last_error = e
            print(f"Download attempt failed ({e}), trying next client...")
            continue

        if not _is_video_track_static(filepath):
            last_error = None
            break
        print(f"Downloaded video track looks frozen/blank, retrying with next client: {filepath}")
        last_error = RuntimeError(f"All attempts yielded a frozen/blank video track: {filepath}")

    if last_error is not None:
        raise last_error

    info["_filepath"] = filepath
    return info


def unique_download_path(stem: str, ext: str, src: Optional[str] = None) -> tuple[str, str]:
    """Sanitize a filename stem into a video_id and pick a collision-free path
    under DOWNLOAD_DIR for it. If src is given and already points at the
    chosen path (or a prior collision-resolved path points at the same file),
    that existing file is treated as already in place rather than a clash."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    video_id = re.sub(r"[^A-Za-z0-9_-]", "_", stem) or "local"
    dest = os.path.join(DOWNLOAD_DIR, f"{video_id}{ext}")
    base_video_id = video_id
    counter = 1
    while os.path.exists(dest) and not (src and os.path.samefile(dest, src)):
        video_id = f"{base_video_id}_{counter}"
        dest = os.path.join(DOWNLOAD_DIR, f"{video_id}{ext}")
        counter += 1
    return video_id, dest


def use_local_file(path: str, title: Optional[str] = None) -> dict:
    """Use an already-downloaded video file instead of fetching one via
    yt-dlp — for feeding in a higher-quality file grabbed with a third-party
    tool (e.g. Cobalt, 4K Video Downloader) when yt-dlp itself is capped.
    Returns the same shape as download_video: {_filepath, id, title}."""
    src = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(src):
        raise FileNotFoundError(f"Local file not found: {src}")

    stem = os.path.splitext(os.path.basename(src))[0]
    ext = os.path.splitext(src)[1] or ".mp4"

    # Copy into DOWNLOAD_DIR so the rest of the pipeline (and any later
    # send-to-resolve call) has a stable path independent of where the user's
    # original file lives.
    video_id, dest = unique_download_path(stem, ext, src=src)
    if not os.path.exists(dest):
        shutil.copy2(src, dest)

    return {"_filepath": dest, "id": video_id, "title": title or stem}


def transcribe(filepath: str, model_size: str = WHISPER_MODEL_SIZE) -> list[dict]:
    """Transcribe with faster-whisper, returning a list of segments with word-level timestamps."""
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        filepath,
        word_timestamps=True,
        # Long-form audio (e.g. a multi-hour church service) often has
        # extended music/silence/crowd-noise stretches. Whisper can get stuck
        # in a repetition/hallucination loop decoding those — each stuck
        # segment can take drastically longer than normal, and with
        # condition_on_previous_text on, a bad segment poisons the ones after
        # it. vad_filter skips non-speech audio before it reaches the decoder
        # at all; disabling conditioning stops one bad segment from
        # compounding into a multi-hour hang.
        vad_filter=True,
        condition_on_previous_text=False,
    )

    print(f"Detected language '{info.language}' (p={info.language_probability:.2f})")

    results = []
    for segment in segments:
        words = [
            {"word": w.word.strip(), "start": w.start, "end": w.end, "prob": w.probability}
            for w in (segment.words or [])
        ]
        results.append(
            {
                "id": segment.id,
                "start": segment.start,
                "end": segment.end,
                "text": segment.text.strip(),
                "words": words,
            }
        )
        
    return results


def main():
    parser = argparse.ArgumentParser(description="Download + transcribe a YouTube video.")
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument("--model", default=WHISPER_MODEL_SIZE, help="faster-whisper model size")
    args = parser.parse_args()

    print(f"Downloading: {args.url}")
    info = download_video(args.url)
    filepath = info["_filepath"]
    print(f"Downloaded to {filepath} ({info.get('title')}, {info.get('duration')}s)")

    print(f"Transcribing with faster-whisper ({args.model}) ...")
    segments = transcribe(filepath, args.model)

    print(f"\n{len(segments)} segments:\n")
    for seg in segments:
        print(f"[{seg['start']:.2f} - {seg['end']:.2f}] {seg['text']}")


if __name__ == "__main__":
    sys.exit(main())
