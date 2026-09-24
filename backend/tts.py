"""
TTS stage (planned pipeline step 2): converts generated commentary text into
narrated audio with word-level timestamps, so the commentary can be captioned
in sync the same way the sermon clip is. Requires ELEVENLABS_API_KEY.
"""

import base64
import os

from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

API_KEY = os.environ.get("ELEVENLABS_API_KEY")
# "Brian" - a stable, publicly available ElevenLabs premade voice marketed
# for narration (resonant, calm delivery). Unverified pick made without an
# API key on hand — listen to it in the ElevenLabs voice library and swap
# via ELEVENLABS_VOICE_ID if it's not the right fit for the channel.
VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "nPczCjzI2devNBz1zQrb")
MODEL_ID = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")


def _words_from_char_alignment(alignment) -> list[dict]:
    """ElevenLabs returns character-level timestamps (parallel arrays), not
    word-level — regroup into words split on whitespace so this lines up with
    whisper's word-dict shape ({"word","start","end"}) that captions.py's
    karaoke renderer already expects."""
    words = []
    current_chars: list[str] = []
    current_start = None
    prev_end = 0.0
    for ch, start, end in zip(
        alignment.characters,
        alignment.character_start_times_seconds,
        alignment.character_end_times_seconds,
    ):
        if ch.isspace():
            if current_chars:
                words.append({"word": "".join(current_chars), "start": current_start, "end": prev_end})
                current_chars = []
                current_start = None
            continue
        if current_start is None:
            current_start = start
        current_chars.append(ch)
        prev_end = end
    if current_chars:
        words.append({"word": "".join(current_chars), "start": current_start, "end": prev_end})
    return words


def synthesize_with_timestamps(text: str, output_path: str) -> dict:
    """Generate narration audio for `text`, save it to output_path (mp3), and
    return {"path", "duration", "words"} — words shaped like whisper's word
    dicts so captions.generate_commentary_ass can render them unchanged."""
    if not API_KEY:
        raise RuntimeError(
            "ELEVENLABS_API_KEY is not set — add it to backend/.env to generate commentary narration."
        )

    client = ElevenLabs(api_key=API_KEY)
    response = client.text_to_speech.convert_with_timestamps(
        voice_id=VOICE_ID,
        text=text,
        model_id=MODEL_ID,
        output_format="mp3_44100_128",
    )

    audio_bytes = base64.b64decode(response.audio_base_64)
    with open(output_path, "wb") as f:
        f.write(audio_bytes)

    alignment = response.normalized_alignment or response.alignment
    words = _words_from_char_alignment(alignment) if alignment else []
    duration = alignment.character_end_times_seconds[-1] if alignment and alignment.character_end_times_seconds else 0.0

    return {"path": output_path, "duration": duration, "words": words}


if __name__ == "__main__":
    import sys

    text = sys.argv[1] if len(sys.argv) > 1 else "This is a test of the commentary narration voice."
    out = synthesize_with_timestamps(text, "commentary_test.mp3")
    print(f"Saved {out['path']} ({out['duration']:.2f}s)")
    for w in out["words"]:
        print(f"  [{w['start']:.2f}-{w['end']:.2f}] {w['word']}")
