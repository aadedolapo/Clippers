import os
import tempfile

# Point Clippers at a throwaway home folder before any test imports it, so
# tests never read or write the real ~/Clippers (or its .env).
os.environ["CLIPPERS_HOME"] = tempfile.mkdtemp(prefix="clippers-test-")


def seg(start, end, text="word word", speaker=None):
    """A whisper-shaped segment with evenly spaced words."""
    words_text = text.split()
    step = (end - start) / len(words_text)
    words = [{"word": w, "start": start + i * step, "end": start + (i + 1) * step, "prob": 1.0}
             for i, w in enumerate(words_text)]
    s = {"id": 0, "start": start, "end": end, "text": text, "words": words}
    if speaker:
        s["speaker"] = speaker
    return s
