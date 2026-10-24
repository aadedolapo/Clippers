import shutil
import subprocess

import pytest

from clippers import audio_energy

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def test_loads_audio_from_an_mp4(tmp_path):
    """Videos are mp4s, which librosa 1.0 can no longer read itself; the
    loader decodes them with ffmpeg instead."""
    path = tmp_path / "tone.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=44100",
         "-c:a", "aac", str(path)],
        check=True,
    )
    y, sr = audio_energy.load_audio(str(path))
    assert sr == 44100
    assert abs(len(y) / sr - 2.0) < 0.1

    energy = audio_energy.analyze_energy(str(path))
    assert abs(energy["duration"] - 2.0) < 0.1
    assert 0.0 <= audio_energy.window_energy_score(energy, 0, 2) <= 1.0
