from __future__ import annotations

import os
from pathlib import Path

HOME = Path.home()
ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = HOME / ".config" / "homelab" / "paperless.env"
# Project-local overrides (gitignored): endpoints, hosts. See .env.example.
LOCAL_ENV_FILE = ROOT / ".env"


def _load_env():
    for env_file in (LOCAL_ENV_FILE, ENV_FILE):
        if not env_file.exists():
            continue
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env()

PAPERLESS_URL = os.environ.get("PAPERLESS_URL", "http://localhost:8000").rstrip("/")
PAPERLESS_TOKEN = os.environ.get("PAPERLESS_TOKEN", "")

# Local llama.cpp / llama-swap (OpenAI-compatible) server.
LLM_URL = os.environ.get("PINTAKE_LLM_URL", "http://localhost:9292/v1")
LLM_MODEL = os.environ.get("PINTAKE_LLM_MODEL", "qwen3-vl-30b-a3b")
LLM_VISION_MODEL = os.environ.get("PINTAKE_LLM_VISION_MODEL", "qwen3-vl-30b-a3b")

# Where filed originals are parked. Kept inside Downloads (same volume, so moves
# are renames) but out of the git repo, which should not hold 0.4 GB of PDFs.
ARCHIVE_DIR = Path(os.environ.get(
    "PINTAKE_ARCHIVE", HOME / "Downloads" / "archived-paperless")).expanduser()

DB_PATH = Path(os.environ.get("PINTAKE_DB", ROOT / "state.db"))
CACHE_DIR = Path(os.environ.get("PINTAKE_CACHE", ROOT / "cache"))

# File types we consider for ingestion.
DOC_EXTS = {".pdf", ".docx", ".doc", ".odt", ".rtf"}
IMG_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}

# Text volume below which a PDF is presumed to be a scan needing OCR.
OCR_TEXT_THRESHOLD = 180

PAPERLESS_HEADERS = {
    "Authorization": f"Token {PAPERLESS_TOKEN}",
    "Accept": "application/json",
}
