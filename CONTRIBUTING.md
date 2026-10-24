# Contributing to Clippers

Thanks for helping out. Bug reports, fixes and new ideas are all welcome.

## Setting up

You need Python 3.9–3.13 and [ffmpeg](https://ffmpeg.org/) on your `PATH`.

```bash
git clone https://github.com/aadedolapo/Clippers.git
cd Clippers
python3.13 -m venv venv         # any Python 3.9–3.13
source venv/bin/activate
pip install -e ".[dev]"          # add ,diarization to work on speaker diarization
clippers init                    # creates ~/Clippers and a .env to fill in
```

To keep development data apart from your real clips, point Clippers at another folder:

```bash
export CLIPPERS_HOME=~/Clippers-dev
```

## Before you open a pull request

```bash
ruff check src tests     # lint (ruff check --fix sorts imports for you)
pytest                   # tests; they run against a throwaway home folder
```

CI runs both on Python 3.9 and 3.13.

- **Keep it working on Python 3.9.** Use `Optional[str]` rather than `str | None`.
- **Add a test** for logic you change. Anything that doesn't need ffmpeg, Resolve or an API key should be testable; see `tests/test_send_fallback.py` for faking those out.
- **If you change scoring** (`WEIGHTS` or a prompt), run `clippers eval` before and after and put both numbers in the pull request.
- **Match the surrounding code**: plain functions, comments that explain *why*, user-facing errors that say what to do next.
- **Update the README** when you change something a user would notice.

## Layout

| Path | What's there |
|---|---|
| `src/clippers/server.py` | FastAPI app and every endpoint |
| `src/clippers/cli.py` | the `clippers` command |
| `src/clippers/paths.py` | where data lives (`~/Clippers` or `CLIPPERS_HOME`) |
| `src/clippers/channel.py` | channel description and lanes from `.env` |
| `src/clippers/static/index.html` | the whole front end, one file, no build step |
| `tests/` | pytest suite |

The README's **How it works** section explains the pipeline.

## Reporting bugs

Open an issue with:
- what you did and what happened;
- the output of `clippers doctor`;
- your OS, and your DaVinci Resolve version if the bug involves Resolve.

Leave out API keys and anything from your `.env`.
