"""Clippers: find the short clips worth posting in a long video, frame them
for vertical, and send them to DaVinci Resolve or render them as mp4s."""

import os

from dotenv import load_dotenv

__version__ = "0.1.0"

# Settings come from a .env in the current folder (handy when developing in
# a checkout), then from the Clippers home folder. The first file found wins
# for each variable, and real environment variables beat both. Loaded here
# so every module sees the same settings however it's imported.
load_dotenv(os.path.join(os.getcwd(), ".env"))

from clippers import paths  # noqa: E402  (CLIPPERS_HOME may come from the .env above)

load_dotenv(paths.ENV_FILE)
