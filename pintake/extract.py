"""Text extraction: native PDF text first, OCR fallback for scans."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import OCR_TEXT_THRESHOLD

MAX_CHARS = 12000
HEAD_PAGES = 4   # first N pages carry title/correspondent/date
TAIL_PAGES = 1   # last page often carries totals/signatures


def _run(cmd, timeout=180):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def page_count(path) -> int | None:
    if not shutil.which("pdfinfo"):
        return None
    p = _run(["pdfinfo", str(path)], timeout=60)
    m = re.search(r"^Pages:\s+(\d+)", p.stdout, re.M)
    return int(m.group(1)) if m else None


def squeeze(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def pdf_text(path, pages=None) -> str:
    cmd = ["pdftotext", "-layout", "-q"]
    if pages:
        cmd += ["-f", str(pages[0]), "-l", str(pages[1])]
    cmd += [str(path), "-"]
    p = _run(cmd)
    return squeeze(p.stdout or "")


def pdf_sampled_text(path) -> tuple[str, int | None]:
    """Head pages + tail page, so long statements stay within the model budget."""
    n = page_count(path)
    if not n:
        return pdf_text(path)[:MAX_CHARS], n
    if n <= HEAD_PAGES + TAIL_PAGES:
        return pdf_text(path)[:MAX_CHARS], n
    head = pdf_text(path, (1, HEAD_PAGES))
    tail = pdf_text(path, (n, n))
    joined = f"{head}\n\n[... pages {HEAD_PAGES + 1}-{n - 1} omitted ...]\n\n{tail}"
    return joined[:MAX_CHARS], n


def ocr_pdf(path, max_pages=3) -> str:
    """Render the first pages and run tesseract. Used when there is no text layer."""
    if not (shutil.which("pdftoppm") and shutil.which("tesseract")):
        return ""
    with tempfile.TemporaryDirectory() as td:
        stem = Path(td) / "pg"
        _run(["pdftoppm", "-r", "200", "-gray", "-f", "1", "-l", str(max_pages),
              "-png", str(path), str(stem)], timeout=300)
        chunks = []
        for img in sorted(Path(td).glob("pg*.png")):
            p = _run(["tesseract", str(img), "stdout", "--psm", "1"], timeout=300)
            chunks.append(p.stdout)
    return squeeze("\n".join(chunks))[:MAX_CHARS]


def ocr_image(path) -> str:
    if not shutil.which("tesseract"):
        return ""
    p = _run(["tesseract", str(path), "stdout", "--psm", "1"], timeout=300)
    return squeeze(p.stdout)[:MAX_CHARS]


def office_text(path) -> str:
    """docx/doc via textutil (macOS) — good enough for classification."""
    if not shutil.which("textutil"):
        return ""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out.txt"
        _run(["textutil", "-convert", "txt", "-output", str(out), str(path)], timeout=180)
        if out.exists():
            return squeeze(out.read_text(errors="replace"))[:MAX_CHARS]
    return ""


def extract(path) -> dict:
    """-> {text, pages, ocr, chars}"""
    path = Path(path)
    ext = path.suffix.lower()
    pages, ocr = None, 0

    if ext == ".pdf":
        text, pages = pdf_sampled_text(path)
        if len(text) < OCR_TEXT_THRESHOLD:
            ocr_text = ocr_pdf(path)
            if len(ocr_text) > len(text):
                text, ocr = ocr_text, 1
    elif ext in {".docx", ".doc", ".odt", ".rtf"}:
        text = office_text(path)
    elif ext in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}:
        text, ocr = ocr_image(path), 1
    else:
        text = ""

    return {"text": text, "pages": pages, "ocr": ocr, "chars": len(text)}


def render_page(path, page=1, dpi=120) -> bytes | None:
    """Render one PDF page to PNG bytes, for the vision fallback."""
    if not shutil.which("pdftoppm"):
        return None
    with tempfile.TemporaryDirectory() as td:
        stem = Path(td) / "pg"
        _run(["pdftoppm", "-r", str(dpi), "-f", str(page), "-l", str(page),
              "-png", "-singlefile", str(path), str(stem)], timeout=180)
        out = Path(f"{stem}.png")
        return out.read_bytes() if out.exists() else None


def page_image(path) -> bytes | None:
    """PNG bytes for a document's first page, whatever the source format."""
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        return render_page(p)
    if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}:
        return p.read_bytes()
    return None
