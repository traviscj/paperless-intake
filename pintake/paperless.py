"""Paperless-ngx API client: taxonomy, dedupe, upload."""
from __future__ import annotations

import json
import os
import subprocess
import time

from .config import PAPERLESS_HEADERS, PAPERLESS_URL
from .http import get_json, post_json, post_multipart, request

SSH_HOST = os.environ.get("PINTAKE_PAPERLESS_SSH", "paperless-host")
CONTAINER = os.environ.get("PINTAKE_PAPERLESS_CONTAINER", "paperless-webserver-1")

ENDPOINTS = {
    "tags": "tags",
    "correspondents": "correspondents",
    "document_types": "document_types",
    "storage_paths": "storage_paths",
    "custom_fields": "custom_fields",
}


def _paged(endpoint):
    url = f"{PAPERLESS_URL}/api/{endpoint}/?page_size=250"
    out = []
    while url:
        d = get_json(url, PAPERLESS_HEADERS)
        out.extend(d["results"])
        url = d.get("next")
    return out


def taxonomy() -> dict:
    """All name->id maps Paperless knows about."""
    tax = {}
    for key, ep in ENDPOINTS.items():
        tax[key] = _paged(ep)
    return tax


def name_map(items):
    return {i["name"].strip().lower(): i["id"] for i in items}


def create(endpoint, payload):
    return post_json(f"{PAPERLESS_URL}/api/{endpoint}/", payload, PAPERLESS_HEADERS)


def ensure(endpoint, name, cache, extra=None):
    """Get-or-create a tag/correspondent/document_type by name. Returns id."""
    key = name.strip().lower()
    if key in cache:
        return cache[key]
    payload = {"name": name.strip()}
    if extra:
        payload.update(extra)
    obj = create(endpoint, payload)
    cache[key] = obj["id"]
    return obj["id"]


def fetch_checksums():
    """Pull (checksum, id, original_filename, title) for every stored document.

    Paperless does not expose the checksum over the REST API, so read it out of
    the container's ORM. Returns [] if the host is unreachable.
    """
    code = (
        "from documents.models import Document;import json,sys;"
        "sys.stdout.write(json.dumps([[d.checksum,d.id,d.original_filename,d.title] "
        "for d in Document.objects.all()]))"
    )
    cmd = [
        "ssh", SSH_HOST, "docker", "exec", CONTAINER,
        "python", "manage.py", "shell", "-c", json.dumps(code),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        raise RuntimeError(f"checksum fetch failed: {p.stderr[-400:]}")
    start = p.stdout.find("[")
    return json.loads(p.stdout[start:])


def sync_checksums(con):
    rows = fetch_checksums()
    now = time.time()
    con.executemany(
        "INSERT OR REPLACE INTO paperless_docs(checksum,doc_id,orig,title,synced) "
        "VALUES (?,?,?,?,?)",
        [(r[0], r[1], r[2], r[3], now) for r in rows],
    )
    con.commit()
    return len(rows)


def upload(path, *, title=None, created=None, correspondent=None, document_type=None,
           tags=None, storage_path=None):
    """POST a document. Returns the consume task UUID string."""
    fields = {
        "title": title,
        "created": created,
        "correspondent": correspondent,
        "document_type": document_type,
        "storage_path": storage_path,
        "tags": [str(t) for t in (tags or [])],
    }
    # PostDocumentSerializer.custom_fields is a PrimaryKeyRelatedField, so it can
    # only attach empty fields here. Values are PATCHed in the finalize stage,
    # once the consumer has turned the upload into a real document.
    fields = {k: v for k, v in fields.items() if v not in (None, [], "")}
    status, body = post_multipart(
        f"{PAPERLESS_URL}/api/documents/post_document/",
        fields, [("document", path)], PAPERLESS_HEADERS,
    )
    text = body.decode("utf-8", "replace").strip()
    if status >= 400:
        raise RuntimeError(f"upload {status}: {text[:400]}")
    return text.strip('"')


def patch_document(doc_id, payload):
    body = json.dumps(payload).encode()
    h = dict(PAPERLESS_HEADERS)
    h["Content-Type"] = "application/json"
    status, out = request("PATCH", f"{PAPERLESS_URL}/api/documents/{doc_id}/", h, body)
    if status >= 400:
        raise RuntimeError(f"PATCH {doc_id} -> {status}: {out[:300]!r}")
    return json.loads(out)


def task_status(task_id):
    d = get_json(f"{PAPERLESS_URL}/api/tasks/?task_id={task_id}", PAPERLESS_HEADERS)
    rows = d if isinstance(d, list) else d.get("results", [])
    return rows[0] if rows else None
