import os, subprocess, tempfile
from clippers import reframe
ASS = ("[Script Info]\nPlayResX: 64\nPlayResY: 64\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize\n"
       "Style: Default,Arial,20\n[Events]\nFormat: Layer, Start, End, Style, Text\n"
       "Dialogue: 0,0:00:00.00,0:00:01.00,Default,HI\n")
base = tempfile.mkdtemp()
print("base", base)
for name in ["plain", "it's", "a, b", "[odd]", "x; y", "sp ace", "it's, [odd]; name"]:
    d = os.path.join(base, name); os.makedirs(d)
    ass = os.path.join(d, "t.ass"); open(ass, "w").write(ASS)
    for label, vf in [("ass", f"ass={reframe.filter_path(ass)}"),
                      ("ass+fontsdir", f"ass={reframe.filter_path(ass)}:fontsdir={reframe.filter_path(d)}"),
                      ("filename=", f"ass=filename={reframe.filter_path(ass)}")]:
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=black:s=64x64:d=0.2",
                            "-vf", vf, os.path.join(base, "o.mp4")], capture_output=True, text=True)
        print(f"{name!r:24} {label:13} rc={r.returncode} {r.stderr.strip().splitlines()[:1]}")
