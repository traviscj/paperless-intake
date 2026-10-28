"""Snap model output onto the existing Paperless taxonomy.

The model reliably reads documents but drifts on naming: it invents
`tax_year:2023` where the archive uses `TaxYear:2023`, and calls a bank by a
different legal name than the archive does. Everything here is about keeping
the archive's existing conventions authoritative.
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path

# Household-specific tables (people, properties, own entities, aliases) live in
# a local, gitignored household.toml — see household.py / household.example.toml.
from .household import (BANNED_TAGS, BULK_SERIES, CORRESPONDENT_ALIASES,
                        CORRESPONDENT_PATTERNS, HOUSEHOLD_RE, PERSON_GUARDS,
                        PERSON_TAGS, PROPERTIES, PROPERTY_ENTITY)

# Namespaced tags the archive uses with a fixed spelling.
NAMESPACES = {"taxyear": "TaxYear", "tax_year": "TaxYear",
              "taxform": "TaxForm", "tax_form": "TaxForm"}

# Tags the model over-applies; only allowed when the document really is that.
GUARDED_TAGS = {
    "tax1098": re.compile(r"\b1098\b", re.I),
    "taxes": re.compile(r"\b(tax|irs|1040|1099|w-?2|schedule [a-e])\b", re.I),
    "leases": re.compile(r"\blease\b", re.I),
    "contracts": re.compile(r"\b(contract|agreement|addendum|rider)\b", re.I),
    "warranty": re.compile(r"\bwarrant(y|ies)\b", re.I),
    "estimates": re.compile(r"\b(estimate|quote|proposal|bid)\b", re.I),
    "photos": re.compile(r"\bphoto", re.I),
    # `birthcert` is one specific document, not a general "identity" bucket —
    # the model reached for it on a driver's licence and a social security card.
    "birthcert": re.compile(r"\bbirth certificate\b", re.I),
}

# Document types the model coins that the archive already has a name for.
DOCTYPE_ALIASES = {
    "financial statement": "Statement",
    "bank statement": "Statement",
    "account statement": "Statement",
    "loan statement": "Statement",
    "trade confirmation": "Statement",
    "pay stub": "Statement",
    "paystub": "Statement",
    "work order": "Work Order",
    "notice": "Letter",
    "correspondence": "Letter",
    "agreement": "Contract",
    "certificate of identity": "Identification",
    "identification document": "Identification",
    "identity document": "Identification",
    "id card": "Identification",
    "drivers license": "Driver's License",
    "driver license": "Driver's License",
    "social security": "Social Security Card",
}

SUFFIX_RE = re.compile(r"[,.]?\s*\b(inc|llc|l\.l\.c|ltd|co|corp|company|companies|"
                       r"incorporated|n\.a|na|plc)\b\.?", re.I)
ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.I)


def _key(s: str) -> str:
    """Aggressive comparison key: lowercase, no punctuation, no corp suffix."""
    s = SUFFIX_RE.sub(" ", s.lower())
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return ARTICLE_RE.sub("", " ".join(s.split()))


def snap(name: str, existing: list[str], cutoff: float = 0.86) -> tuple[str, bool]:
    """Return (canonical_name, matched_existing)."""
    if not name:
        return name, False
    by_key = {_key(e): e for e in existing}
    k = _key(name)
    if k in by_key:
        return by_key[k], True
    close = difflib.get_close_matches(k, list(by_key), n=1, cutoff=cutoff)
    if close:
        return by_key[close[0]], True
    # A multi-word name that is a whole-word prefix of an existing one is the
    # same party under a shorter label: "PNC Bank" -> "PNC Bank, National
    # Association". Single words are too ambiguous to fold this way.
    kw = k.split()
    if len(kw) >= 2:
        for ek, ename in by_key.items():
            ekw = ek.split()
            if len(ekw) > len(kw) and ekw[:len(kw)] == kw:
                return ename, True
    return name.strip(), False


# Plain tags the model coins for things the archive already names.
TAG_ALIASES = {
    "tax1098": "TaxForm:1098",
    "tax1098-sa": "TaxForm:1098-SA",
    "tax1099-sa": "TaxForm:1099-SA",
    "tax5498-sa": "TaxForm:5498-SA",
    "tax1095-c": "TaxForm:1095-C",
    "w2": "TaxForm:W2",
    "w-2": "TaxForm:W2",
}


def consolidate(names: dict[str, int], existing: list[str],
                cutoff: float = 0.88) -> dict[str, str]:
    """Cluster newly-proposed names among THEMSELVES.

    `snap` only compares a name against what Paperless already holds, so two
    spellings that are both new ("Acme Realty" / "Acme Realty LLC")
    each pass through untouched and create twin correspondents. Here the most
    frequently proposed spelling wins; a name that is a whole-word prefix of a
    more common one folds into it ("Acme" -> "Acme Apartments LLC").
    Returns {name: canonical_name} for names that should change.
    """
    existing_keys = {_key(e) for e in existing}
    fresh = [(n, c) for n, c in names.items() if _key(n) not in existing_keys]
    fresh.sort(key=lambda x: (-x[1], len(x[0])))

    canon: dict[str, str] = {}
    reps: list[tuple[str, str]] = []  # (key, canonical name)
    for name, _count in fresh:
        k = _key(name)
        hit = None
        for rk, rname in reps:
            if k == rk or difflib.SequenceMatcher(None, k, rk).ratio() >= cutoff:
                hit = rname
                break
            kw, rkw = k.split(), rk.split()
            if kw and rkw and (kw[:len(rkw)] == rkw or rkw[:len(kw)] == kw):
                hit = rname  # one is a whole-word prefix of the other
                break
        if hit:
            canon[name] = hit
        else:
            reps.append((k, name))
    return {n: c for n, c in canon.items() if n != c}


def canon_tag(tag: str) -> str | None:
    tag = tag.strip()
    if not tag:
        return None
    if ":" in tag:
        ns, _, val = tag.partition(":")
        ns_c = NAMESPACES.get(ns.strip().lower().replace(" ", "_"))
        if ns_c:
            val = val.strip().upper().replace(" ", "-")
            return f"{ns_c}:{val}"
    low = tag.lower()
    if low in TAG_ALIASES:
        return TAG_ALIASES[low]
    if low in PROPERTIES:
        return PROPERTIES[low]
    if low in PERSON_TAGS:
        return low
    return tag


def normalize(meta: dict, tax: dict, *, text: str = "", max_topical: int = 4) -> dict:
    """Rewrite a model result in the archive's own vocabulary. Mutates a copy."""
    m = dict(meta)
    existing_tags = [t["name"] for t in tax["tags"]]
    existing_corr = [c["name"] for c in tax["correspondents"]]
    existing_types = [d["name"] for d in tax["document_types"]]
    haystack = f"{m.get('title','')} {m.get('summary','')} {text}"

    # --- correspondent ------------------------------------------------
    corr = (m.get("correspondent") or "").strip() or None
    if corr:
        corr = CORRESPONDENT_ALIASES.get(_key(corr), corr)
        corr = CORRESPONDENT_ALIASES.get(corr.lower(), corr)
        for rx, canonical in CORRESPONDENT_PATTERNS:
            if rx.search(_key(corr)):
                corr = canonical
                break
        corr, _ = snap(corr, existing_corr)
        if HOUSEHOLD_RE.search(_key(corr)):
            corr = None  # never the household itself
    m["correspondent"] = corr

    # --- document type -------------------------------------------------
    if m.get("document_type"):
        dt = m["document_type"].strip()
        dt = DOCTYPE_ALIASES.get(dt.lower(), dt)
        m["document_type"], _ = snap(dt, existing_types)

    # --- properties ----------------------------------------------------
    props = m.get("properties") or ([m["property"]] if m.get("property") else [])
    props = [PROPERTIES[p.lower()] for p in props if p and p.lower() in PROPERTIES]
    m["properties"] = sorted(set(props))
    m.pop("property", None)

    # --- tags ----------------------------------------------------------
    person, topical = [], []
    for raw in m.get("tags") or []:
        t = canon_tag(raw)
        if not t:
            continue
        low = t.lower()
        if low in BANNED_TAGS:
            continue  # the correspondent already carries this
        if low in PROPERTIES or low.startswith(("taxyear:", "taxform:")):
            continue  # derived below, not taken from the free-form list
        if low in PERSON_TAGS:
            guard = PERSON_GUARDS.get(low)
            if guard and not guard.search(haystack):
                continue
            if low not in person:
                person.append(low)
            continue
        guard = GUARDED_TAGS.get(low)
        if guard and not guard.search(haystack):
            continue  # model sprayed a topical tag the document does not support
        t, _ = snap(t, existing_tags, cutoff=0.9)
        if t not in topical:
            topical.append(t)

    derived = list(m["properties"])
    if m.get("tax_year"):
        derived.append(f"TaxYear:{int(m['tax_year'])}")
    for raw in meta.get("tags") or []:
        c = canon_tag(raw) or ""
        if c.startswith("TaxForm:"):
            derived.append(c)

    m["tags"] = person + topical[:max_topical] + sorted(set(derived))
    return m


# Repetitive machine-generated series. These are worth filing individually (they
# substantiate expenses and cost basis) but they outnumber everything else, so
# they carry a tag that lets the default Paperless view exclude them:
#   filter with  -tag:bulkseries
# (series regexes come from household.toml [bulk_series])
BULK_TAG = "bulkseries"


def bulk_series(path: str) -> str | None:
    """Name of the repetitive series this file belongs to, if any."""
    for name, rx in BULK_SERIES.items():
        if rx.search(path):
            return name
    return None


def apply_series(meta: dict, path: str) -> dict:
    """Add the bulkseries tag when the file is part of a repetitive series."""
    if bulk_series(path) and BULK_TAG not in meta.get("tags", []):
        meta = dict(meta)
        meta["tags"] = list(meta["tags"]) + [BULK_TAG]
    return meta


# --- derived custom fields -------------------------------------------------
# A filing tree can encode the account in the filename:
#   bank/acme/1234_2020-12-02_MortgageStatement.pdf -> account 1234
# Utility folders use a property slug there instead, so only 4-digit runs count.
ACCOUNT_RE = re.compile(r"^(\d{4})_\d{4}-\d{2}-\d{2}_")

# Each rental/commercial property may be held by its own entity; the
# property -> entity map comes from household.toml [property_entity].

# A bare "#1234" is far more often a work order, invoice or claim number than a
# unit — "Work Order #4939-1" was being read as unit 4939. So either the word
# unit/apt/suite is present, or it is the Chicago flat form (#1F, #2R).
UNIT_RE = re.compile(
    r"(?<!work order )(?<!invoice )(?<!order )(?<!claim )(?<!check )"
    r"(?:unit|apt|apartment|suite|ste)\.?\s*#?\s*(\d{1,4}[A-Za-z]?)\b"
    r"|#\s?(\d{1,2}[FR])\b", re.I)


def derive_custom_fields(path: str, meta: dict) -> dict:
    """Custom field values that follow from the path or the metadata.

    Only derivations that are certain — a guessed account number or entity is
    worse than an empty field, because it looks authoritative.
    """
    out: dict = {}
    name = Path(path).name

    m = ACCOUNT_RE.match(name)
    if m:
        out["Account"] = m.group(1)

    props = meta.get("properties") or []
    if len(props) == 1 and props[0] in PROPERTY_ENTITY:
        out["Entity"] = PROPERTY_ENTITY[props[0]]

    um = UNIT_RE.search(meta.get("title") or "")
    if um:
        out["Unit"] = (um.group(1) or um.group(2)).upper()

    if meta.get("tax_year"):
        out["Tax Year"] = int(meta["tax_year"])
    return out
