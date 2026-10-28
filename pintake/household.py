"""Household-specific configuration: people, owned properties, own entities,
correspondent aliases. None of this belongs in the code — it lives in a local
TOML file that is gitignored.

Lookup order: $PINTAKE_HOUSEHOLD, then <project>/household.toml, then the
committed household.example.toml (fictional data, so the pipeline still runs).
"""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from .config import ROOT


def _path() -> Path:
    env = os.environ.get("PINTAKE_HOUSEHOLD")
    if env:
        return Path(env).expanduser()
    local = ROOT / "household.toml"
    return local if local.exists() else ROOT / "household.example.toml"


PATH = _path()
_H = tomllib.loads(PATH.read_text()) if PATH.exists() else {}

# Free-text context block shown to the model verbatim.
CONTEXT: str = _H.get("context", "").rstrip() + "\n"

# [[people]] name / tag / guard (regex the document must match to keep the tag)
PEOPLE: list[dict] = _H.get("people", [])
PERSON_NAMES = [p["name"] for p in PEOPLE]
PERSON_TAGS = {p["tag"] for p in PEOPLE}
PERSON_GUARDS = {p["tag"]: re.compile(p["guard"], re.I) for p in PEOPLE if p.get("guard")}

# [properties] bare key shown to the model -> canonical Paperless tag
PROPERTY_KEYS: list[str] = list(_H.get("properties", {}))
PROPERTIES: dict[str, str] = {}
for _k, _v in _H.get("properties", {}).items():
    PROPERTIES[_k.lower()] = _v
    PROPERTIES[_v.lower()] = _v
for _k, _v in _H.get("property_aliases", {}).items():
    PROPERTIES[_k.lower()] = _v

# Optional: the residence whose address is printed on everything.
RESIDENCE: dict = _H.get("residence", {})

# canonical property tag -> owning entity (custom field "Entity")
PROPERTY_ENTITY: dict[str, str] = _H.get("property_entity", {})

# Correspondent names that are really the household / its own entities.
_own = _H.get("own_entity_regex", "")
HOUSEHOLD_RE = re.compile(_own, re.I) if _own else re.compile(r"(?!x)x")

BANNED_TAGS: set[str] = {t.lower() for t in _H.get("banned_tags", [])}

CORRESPONDENT_ALIASES: dict[str, str] = _H.get("correspondent_aliases", {})
CORRESPONDENT_PATTERNS = [(re.compile(p["regex"]), p["name"])
                          for p in _H.get("correspondent_patterns", [])]

# [bulk_series] name -> regex over the file path
BULK_SERIES = {k: re.compile(v, re.I) for k, v in _H.get("bulk_series", {}).items()}
