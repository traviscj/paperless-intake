"""Convert file types Paperless cannot consume into PDF.

This Paperless deployment has no Tika/Gotenberg sidecars, so it rejects
Word documents outright ("File type application/msword not supported").
Rather than add two containers for a handful of files, convert locally:
textutil (ships with macOS, reads .doc/.docx/.rtf/.odt) produces HTML, and
headless Chrome prints that to PDF.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from .config import CACHE_DIR

# Types the Paperless consumer accepts as-is.
NATIVE_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".txt"}
CONVERTIBLE_EXTS = {".doc", ".docx", ".odt", ".rtf"}

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def _chrome() -> str | None:
    for c in CHROME_CANDIDATES:
        if Path(c).is_file():
            return c
    return shutil.which("chromium") or shutil.which("google-chrome")


def needs_conversion(path) -> bool:
    return Path(path).suffix.lower() in CONVERTIBLE_EXTS


def to_pdf(path) -> Path:
    """Return a PDF rendering of `path`, cached by content hash."""
    path = Path(path)
    if not needs_conversion(path):
        return path

    digest = hashlib.md5(path.read_bytes()).hexdigest()[:16]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / f"{digest}.pdf"
    if out.exists() and out.stat().st_size > 0:
        return out

    chrome = _chrome()
    if not (shutil.which("textutil") and chrome):
        raise RuntimeError("need textutil and a Chrome/Chromium binary to convert "
                           f"{path.suffix} to PDF")

    html = CACHE_DIR / f"{digest}.html"
    subprocess.run(["textutil", "-convert", "html", "-output", str(html), str(path)],
                   check=True, capture_output=True, timeout=180)
    subprocess.run([chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={out}", html.as_uri()],
                   check=True, capture_output=True, timeout=300)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"conversion produced no PDF for {path.name}")
    html.unlink(missing_ok=True)
    return out


def repair_pdf(path) -> Path:
    """Re-render a PDF that Paperless' Ghostscript PDF/A step rejects.

    IRS fillable forms and some generated PDFs carry structures Ghostscript
    refuses to convert to PDF/A ("Ghostscript PDF/A rendering failed"), which
    fails the whole consume. Re-rendering through poppler produces a plain,
    well-formed PDF that keeps the text layer.
    """
    path = Path(path)
    if not shutil.which("pdftocairo"):
        raise RuntimeError("pdftocairo (poppler) is required to repair PDFs")
    digest = hashlib.md5(path.read_bytes()).hexdigest()[:16]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / f"{digest}-repaired.pdf"
    if out.exists() and out.stat().st_size > 0:
        return out
    subprocess.run(["pdftocairo", "-pdf", str(path), str(out)],
                   check=True, capture_output=True, timeout=600)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"repair produced no PDF for {path.name}")
    return out
