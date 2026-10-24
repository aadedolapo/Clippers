import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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
