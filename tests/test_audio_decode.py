import wave

from faster_whisper.audio import decode_audio


def test_faster_whisper_can_decode_audio(tmp_path):
    """Guards the faster-whisper / PyAV pairing: a PyAV release that breaks
    faster-whisper's decoder (as 19.0 did) fails here instead of on the
    first real transcription."""
    path = tmp_path / "silence.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    audio = decode_audio(str(path), sampling_rate=16000)
    assert len(audio) == 16000
