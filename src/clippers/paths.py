"""
Where Clippers keeps things. Code and bundled assets live in the installed
package; everything the app writes (jobs, downloaded and uploaded videos,
rendered clips, the face-detection model, eval results) lives under one
home folder, ~/Clippers by default or CLIPPERS_HOME if set. The .env file
is read from that folder too (see clippers/__init__.py).
"""

import os

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(PACKAGE_DIR, "static")
FONTS_DIR = os.path.join(PACKAGE_DIR, "assets", "fonts")

HOME = os.path.abspath(os.path.expanduser(os.environ.get("CLIPPERS_HOME") or "~/Clippers"))
ENV_FILE = os.path.join(HOME, ".env")
DATA_DIR = os.path.join(HOME, "data")
JOBS_DIR = os.path.join(DATA_DIR, "jobs")
DOWNLOAD_DIR = os.path.join(HOME, "downloads")
BROLL_DOWNLOAD_DIR = os.path.join(DOWNLOAD_DIR, "broll")
OUTPUT_DIR = os.path.join(HOME, "output")
MODELS_DIR = os.path.join(HOME, "models")
EVAL_DIR = os.path.join(HOME, "eval")
