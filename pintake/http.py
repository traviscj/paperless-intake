"""Tiny stdlib HTTP helpers (no requests dependency)."""
from __future__ import annotations

import json
import mimetypes
import os
import ssl
import urllib.error
import urllib.request
import uuid

_CTX = ssl.create_default_context()


def request(method, url, headers=None, data=None, timeout=120):
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def get_json(url, headers=None, timeout=120):
    status, body = request("GET", url, headers, timeout=timeout)
    if status >= 400:
        raise RuntimeError(f"GET {url} -> {status}: {body[:400]!r}")
    return json.loads(body)


def post_json(url, payload, headers=None, timeout=600):
    h = dict(headers or {})
    h["Content-Type"] = "application/json"
    status, body = request("POST", url, h, json.dumps(payload).encode(), timeout)
    if status >= 400:
        raise RuntimeError(f"POST {url} -> {status}: {body[:600]!r}")
    return json.loads(body)


def post_multipart(url, fields, files, headers=None, timeout=600):
    """fields: {name: str}; files: [(name, filepath)]."""
    boundary = uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        if value is None:
            continue
        vals = value if isinstance(value, (list, tuple)) else [value]
        for v in vals:
            out += f"--{boundary}\r\n".encode()
            out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            out += f"{v}\r\n".encode()
    for name, path in files:
        fn = os.path.basename(path)
        ctype = mimetypes.guess_type(fn)[0] or "application/octet-stream"
        out += f"--{boundary}\r\n".encode()
        out += (
            f'Content-Disposition: form-data; name="{name}"; filename="{fn}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode()
        with open(path, "rb") as fh:
            out += fh.read()
        out += b"\r\n"
    out += f"--{boundary}--\r\n".encode()

    h = dict(headers or {})
    h["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    return request("POST", url, h, bytes(out), timeout)
