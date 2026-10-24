"""The `clippers` command.

    clippers init       create the home folder and a .env to fill in
    clippers serve      start the app at http://127.0.0.1:8000
    clippers doctor     check ffmpeg, the API key, Resolve and optional extras
    clippers report     posted-clip performance by lane, duration and cold open
    clippers eval ...   score-vs-retention eval (see `clippers eval --help`)
"""

import argparse
import os
import shutil
import sys
import warnings
from typing import Optional

from clippers import __version__, paths

ENV_TEMPLATE = os.path.join(paths.PACKAGE_DIR, "assets", "env.example")


def cmd_init(args) -> int:
    for d in (paths.DATA_DIR, paths.DOWNLOAD_DIR, paths.OUTPUT_DIR):
        os.makedirs(d, exist_ok=True)
    if os.path.exists(paths.ENV_FILE):
        print(f"{paths.ENV_FILE} already exists; leaving it as is.")
    else:
        shutil.copyfile(ENV_TEMPLATE, paths.ENV_FILE)
        print(f"Created {paths.ENV_FILE}")
    print(f"\nClippers home: {paths.HOME}")
    print("Next: add your ANTHROPIC_API_KEY to that .env, then run `clippers serve`.")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from clippers.server import app

    print(f"Clippers {__version__} - open http://{args.host}:{args.port} (home: {paths.HOME})")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _check(ok: bool, label: str, hint: str = "") -> bool:
    print(f"  {'ok ' if ok else '-- '} {label}" + (f"\n       {hint}" if hint and not ok else ""))
    return ok


def cmd_doctor(args) -> int:
    print(f"Clippers {__version__}, Python {sys.version.split()[0]}, home {paths.HOME}\n")
    print("Required")
    ok = _check(bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe")), "ffmpeg and ffprobe on PATH",
                "Install ffmpeg (macOS: brew install ffmpeg; Windows: winget install ffmpeg).")
    ok &= _check(bool(os.environ.get("ANTHROPIC_API_KEY")), "ANTHROPIC_API_KEY set",
                 f"Run `clippers init`, then add the key to {paths.ENV_FILE}.")
    home_ok = os.path.isdir(paths.HOME) and os.access(paths.HOME, os.W_OK)
    ok &= _check(home_ok, f"home folder writable ({paths.HOME})", "Run `clippers init`.")

    print("\nOptional")
    from clippers import diarize, resolve_integration

    try:
        resolve_integration._get_project()
        _check(True, "DaVinci Resolve reachable with a project open")
    except Exception as e:
        _check(False, "DaVinci Resolve reachable with a project open",
               f"{e} Without Resolve, clips are still rendered as mp4s.")
    try:
        with warnings.catch_warnings():  # torchaudio deprecation noise on import
            warnings.simplefilter("ignore")
            import pyannote.audio  # noqa: F401
        has_pyannote = True
    except ImportError:
        has_pyannote = False
    _check(has_pyannote, "speaker diarization installed", 'pip install "clippers[diarization]"')
    if has_pyannote:
        _check(diarize.available(), "HF_TOKEN set for diarization", "Add HF_TOKEN to your .env.")
    for key, what in (("PEXELS_API_KEY", "B-roll suggestions"), ("ELEVENLABS_API_KEY", "narrated outro"),
                      ("MUSIC_DIRS", "music beds"), ("SFX_DIR", "whoosh sound effects")):
        _check(bool(os.environ.get(key)), f"{key} set ({what})")
    print("\nAll required checks passed." if ok else "\nFix the required items above, then run `clippers doctor` again.")
    return 0 if ok else 1


def cmd_report(args) -> int:
    from clippers import performance

    performance.print_report()
    return 0


def main(argv: Optional[list] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # eval has its own argument parser; hand it everything after "eval".
    if argv[:1] == ["eval"]:
        from clippers import evaluate

        evaluate.main(argv[1:])
        return 0

    parser = argparse.ArgumentParser(prog="clippers", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"clippers {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the home folder and a .env to fill in").set_defaults(func=cmd_init)
    serve = sub.add_parser("serve", help="start the app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)
    sub.add_parser("doctor", help="check that everything is set up").set_defaults(func=cmd_doctor)
    sub.add_parser("report", help="posted-clip performance report").set_defaults(func=cmd_report)
    sub.add_parser("eval", help="score-vs-retention eval (see `clippers eval --help`)")
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
