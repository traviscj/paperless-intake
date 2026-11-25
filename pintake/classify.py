"""Local-model document classification against the live Paperless taxonomy."""
from __future__ import annotations

import json
import re

from . import household
from .config import LLM_MODEL, LLM_URL
from .http import post_json

# Who the household is and what it owns comes from household.toml (gitignored).
HOUSEHOLD = household.CONTEXT


def _join(xs, conj="or"):
    xs = list(xs)
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + f" {conj} " + xs[-1]


_PEOPLE = _join(household.PERSON_NAMES) if household.PERSON_NAMES else "the household"
_PTAGS = ", ".join(p["tag"] for p in household.PEOPLE)
_PKEYS = ", ".join(household.PROPERTY_KEYS)
_RES = household.RESIDENCE
_RESIDENCE_RULE = (
    f"""  * A MAILING ADDRESS IS NOT A PROPERTY ASSOCIATION. {_RES.get('who', 'The household')} live at
    {_RES['address']}, so that address is printed on nearly every statement
    they receive — brokerage confirmations, pay stubs, W-2s, bank statements,
    medical bills. Those documents get an EMPTY properties list. Only include
    {_RES['key']} when the document is genuinely about the house.
""" if _RES.get("address") else "")

RULES = f"""\
Decide a disposition for the document:
- "file"   : a record worth keeping permanently — tax forms, returns, statements, bills,
             invoices, leases, contracts, closing/loan docs, insurance, legal filings,
             medical records, licenses/certificates, warranties, property financials,
             correspondence with lasting consequence, anything with an account number
             or a signature on it. Work orders, repair invoices and vendor receipts
             for an OWNED property are "file" — they substantiate deductible expenses.
- "review" : possibly worth keeping but ambiguous, low-quality OCR, or you are unsure.
- "skip"   : no lasting record value — product manuals and datasheets, software docs,
             marketing brochures / offering memoranda for properties NOT owned,
             academic papers and articles, web page printouts, blank forms,
             menus, maps, event flyers, shipping labels, duplicate exports.

Rules for the metadata:
- title: human-readable, specific, no file extension, no date-only titles. Follow the
  style of the existing titles you were shown.
- created: the date ON the document (issue/statement/signature date), ISO YYYY-MM-DD.
  Use null if the document states no date. Do not guess from the filename alone.
- correspondent: the OTHER party — the organization or person who issued or sent it.
  Never use {_PEOPLE} as the correspondent. Null if there is none.
- document_type: one of the existing types when one fits; otherwise propose a short
  singular noun phrase.
- tags: BE SPARING. Emit only tags that are literally true of this document.
  * Always include the person tag(s) the document concerns: {_PTAGS}.
  * Then AT MOST THREE topical tags, reusing existing ones where they fit.
  * Do NOT add a tag just because it exists in the list. In particular:
    "taxes"/"tax1098" only for actual tax documents; "leases" only for an actual
    lease; "contracts" only for a signed agreement; "financial" only for money
    documents. A resume, a work order and a utility notice get none of those.
  * Do NOT emit property tags or TaxYear tags — those are derived from the
    `properties` and `tax_year` fields below.
  * DO emit a TaxForm:<form> tag when the document IS a named tax form,
    e.g. TaxForm:1099-B, TaxForm:W2, TaxForm:1098.
- properties: every owned property the document is ABOUT, from {_PKEYS}.
  Empty list if none.
  * A document is "about" a property when it concerns that property itself: its
    purchase or sale, mortgage, lease, tenants, repairs, utilities, insurance,
    property tax, survey, or operating financials.
{_RESIDENCE_RULE}  * A portfolio-wide financial report lists all the properties it covers.
- tax_year: the tax year for tax documents only, otherwise null.
"""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["disposition", "reason", "confidence", "title", "created",
                 "correspondent", "document_type", "tags", "properties",
                 "tax_year", "summary"],
    "properties": {
        "disposition": {"type": "string", "enum": ["file", "review", "skip"]},
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
        "title": {"type": "string"},
        "created": {"type": ["string", "null"], "pattern": r"^\d{4}-\d{2}-\d{2}$"},
        "correspondent": {"type": ["string", "null"]},
        "document_type": {"type": ["string", "null"]},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 7},
        "properties": {
            "type": "array",
            "maxItems": max(1, len(household.PROPERTY_KEYS)),
            "items": {"type": "string", "enum": household.PROPERTY_KEYS},
        },
        "tax_year": {"type": ["integer", "null"]},
        "summary": {"type": "string"},
    },
}


def taxonomy_block(tax):
    def names(key):
        return ", ".join(sorted(i["name"] for i in tax[key])) or "(none)"
    return (
        f"EXISTING TAGS: {names('tags')}\n\n"
        f"EXISTING CORRESPONDENTS: {names('correspondents')}\n\n"
        f"EXISTING DOCUMENT TYPES: {names('document_types')}\n"
    )


def build_prompt(tax, filename, mtime_iso, pages, ocr, text):
    return (
        "You are cataloguing a personal/small-business document archive for "
        "Paperless-ngx.\n\n"
        + HOUSEHOLD + "\n" + taxonomy_block(tax) + "\n" + RULES
        + "\n----- DOCUMENT -----\n"
        f"filename: {filename}\n"
        f"downloaded: {mtime_iso}\n"
        f"pages: {pages if pages is not None else 'unknown'}"
        f"{'  (text recovered by OCR, may be noisy)' if ocr else ''}\n"
        "extracted text:\n"
        f"{text if text.strip() else '(no text could be extracted)'}\n"
        "----- END DOCUMENT -----\n\n"
        "Respond with the JSON object only."
    )


class LLMUnavailable(RuntimeError):
    """The model server cannot serve this model right now. Nothing about the document."""


def preflight(model=None, vision_model=None, url=None) -> list[str]:
    """Make sure the server has the model(s) and can actually load them, before any document is touched.

    A model that is listed but cannot start (its file was moved or deleted) answers every request with an
    instant 500, which would otherwise mark every file in the run as failed in a few seconds.
    """
    from .config import LLM_VISION_MODEL
    from .http import get_json
    base = url or LLM_URL
    want = list(dict.fromkeys([model or LLM_MODEL, vision_model or model or LLM_VISION_MODEL]))
    try:
        have = sorted(m["id"] for m in get_json(f"{base}/models", timeout=15)["data"])
    except Exception as e:  # noqa: BLE001
        raise LLMUnavailable(f"model server {base} is not answering: {e}") from e
    for m in want:
        if m not in have:
            raise LLMUnavailable(f"model '{m}' is not on {base}. It has: {', '.join(have)}. "
                                 "Pick one with --model, or set PINTAKE_LLM_MODEL in .env.")
        try:    # the first real request is what loads it; a broken model fails here, in seconds
            post_json(f"{base}/chat/completions", {"model": m, "max_tokens": 1, "messages": [{"role": "user", "content": "ok"}]}, timeout=600)
        except Exception as e:  # noqa: BLE001
            raise LLMUnavailable(f"model '{m}' is listed on {base} but will not start: {str(e)[:300]}. "
                                 "Its files are probably missing on the server. Use another with --model.") from e
    return want


def infrastructure_error(err: str) -> bool:
    """Is this failure about the server (retry later) rather than about the document (needs a look)?"""
    e = err or ""
    return ("-> 5" in e and "POST " in e) or any(k in e for k in ("URLError", "ConnectionError", "ConnectionRefused", "TimeoutError", "timed out", "RemoteDisconnected"))


def _strip_think(s: str) -> str:
    return re.sub(r"<think>.*?</think>", "", s, flags=re.S).strip()


def classify(tax, *, filename, mtime_iso, pages, ocr, text,
             model=None, url=None, timeout=600):
    prompt = build_prompt(tax, filename, mtime_iso, pages, ocr, text)
    payload = {
        "model": model or LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 1200,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "doc_meta", "strict": True, "schema": SCHEMA},
        },
    }
    resp = post_json(f"{url or LLM_URL}/chat/completions", payload, timeout=timeout)
    content = _strip_think(resp["choices"][0]["message"]["content"])
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"no JSON in model reply: {content[:300]}")
    return json.loads(content[start:end + 1])


def classify_image(tax, *, filename, mtime_iso, image_png: bytes,
                   model=None, url=None, timeout=900):
    """Classify from a rendered page when no text could be extracted.

    A scan with no OCR text leaves the text model guessing from the filename
    alone — that is how an ID card came back as a "social media profile". The
    default model is vision-capable, so show it the page instead.
    """
    import base64

    from .config import LLM_VISION_MODEL

    prompt = build_prompt(
        tax, filename, mtime_iso, pages=None, ocr=0,
        text="(no text layer; the page image is attached — read it directly)",
    )
    b64 = base64.b64encode(image_png).decode()
    payload = {
        "model": model or LLM_VISION_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
        "temperature": 0.1,
        "max_tokens": 1200,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "doc_meta", "strict": True, "schema": SCHEMA},
        },
    }
    resp = post_json(f"{url or LLM_URL}/chat/completions", payload, timeout=timeout)
    content = _strip_think(resp["choices"][0]["message"]["content"])
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"no JSON in model reply: {content[:300]}")
    return json.loads(content[start:end + 1])
