"""Pull document attachments out of Gmail.

The Gmail MCP exposes attachment ids but not their bytes, so this talks to the
Gmail API directly with a read-only OAuth token. Credentials live in
~/.config/homelab/ and the token is cached, so the consent flow runs once.

Setup (one time):
  1. console.cloud.google.com -> new project
  2. APIs & Services -> Library -> enable "Gmail API"
  3. APIs & Services -> Credentials -> Create credentials
     -> OAuth client ID -> Application type: Desktop app
  4. Download the JSON to ~/.config/homelab/gmail_client_secret.json
  5. pintake email-fetch --since 2026-01-01      (opens a browser once)
"""
from __future__ import annotations

import base64
import re
from pathlib import Path

from .config import HOME

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CLIENT_SECRET = HOME / ".config" / "homelab" / "gmail_client_secret.json"
TOKEN_PATH = HOME / ".config" / "homelab" / "gmail_token.json"

WANTED_MIMETYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# Recurring software/subscription receipts. These are real receipts but they
# arrive monthly forever and would swamp the archive; card statements already
# record the spend.
DENY_SENDERS = [
    "invoice+statements@mail.anthropic.com",
    "noreply@github.com",
    "@stripe.com",
    "info@reia.org",
    "@email.apple.com",
    "notifications@",
    "no-reply@accounts.google.com",
    "@e.progressive.com",
    "team@",
    "newsletter@",
]

# Attachments smaller than this are almost always signature logos or tracking
# pixels rather than documents.
MIN_ATTACHMENT_BYTES = 12_000

SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str, max_len: int = 48) -> str:
    out = SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return out[:max_len].strip("-") or "message"


def service(client_secret: Path | None = None, token: Path | None = None):
    """Authorised Gmail client, running the consent flow only if needed."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    client_secret = Path(client_secret or CLIENT_SECRET)
    token = Path(token or TOKEN_PATH)

    creds = None
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not client_secret.exists():
                raise FileNotFoundError(
                    f"{client_secret} not found — see the setup steps in "
                    "pintake/mailfetch.py or `pintake email-setup`."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_secret), SCOPES)
            creds = flow.run_local_server(port=0)
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(creds.to_json())
        token.chmod(0o600)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def denied(sender: str, extra_deny: tuple[str, ...] = ()) -> bool:
    low = (sender or "").lower()
    return any(d in low for d in list(DENY_SENDERS) + list(extra_deny))


def list_message_ids(svc, query: str, max_messages: int = 1000):
    ids, page = [], None
    while len(ids) < max_messages:
        resp = (svc.users().messages()
                .list(userId="me", q=query, pageToken=page, maxResults=500)
                .execute())
        ids.extend(m["id"] for m in resp.get("messages", []))
        page = resp.get("nextPageToken")
        if not page:
            break
    return ids[:max_messages]


def _headers(msg) -> dict:
    return {h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])}


def _walk_parts(part):
    yield part
    for sub in part.get("parts", []) or []:
        yield from _walk_parts(sub)


def fetch(svc, out_dir: Path, query: str, max_messages: int = 1000,
          extra_deny: tuple[str, ...] = (), dry_run: bool = False) -> dict:
    """Download document attachments matching `query`. Returns a tally."""
    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    ids = list_message_ids(svc, query, max_messages)
    tally = {"messages": len(ids), "skipped_sender": 0, "saved": 0,
             "too_small": 0, "wrong_type": 0, "existed": 0}

    for mid in ids:
        msg = svc.users().messages().get(
            userId="me", id=mid, format="full").execute()
        hdr = _headers(msg)
        sender = hdr.get("from", "")
        if denied(sender, extra_deny):
            tally["skipped_sender"] += 1
            continue

        date = (hdr.get("date", "") or "")
        # Gmail's internalDate is unambiguous; the Date header is not.
        import datetime as dt
        stamp = dt.datetime.fromtimestamp(
            int(msg["internalDate"]) / 1000).strftime("%Y-%m-%d")
        domain = sender.split("@")[-1].strip("<> ").split(">")[0] or "unknown"
        subject = hdr.get("subject", "")

        for part in _walk_parts(msg.get("payload", {})):
            body = part.get("body", {})
            att_id = body.get("attachmentId")
            if not att_id:
                continue
            if part.get("mimeType") not in WANTED_MIMETYPES:
                tally["wrong_type"] += 1
                continue
            if body.get("size", 0) < MIN_ATTACHMENT_BYTES:
                tally["too_small"] += 1
                continue

            orig = part.get("filename") or "attachment.pdf"
            ext = Path(orig).suffix.lower() or ".pdf"
            name = f"{stamp}-{_slug(domain, 24)}-{_slug(subject)}{ext}"
            dest = out_dir / name
            if dest.exists():
                tally["existed"] += 1
                continue
            if dry_run:
                print(f"  {stamp}  {sender[:38]:38} {orig[:34]:34} -> {name}")
                tally["saved"] += 1
                continue
            att = (svc.users().messages().attachments()
                   .get(userId="me", messageId=mid, id=att_id).execute())
            dest.write_bytes(base64.urlsafe_b64decode(att["data"]))
            tally["saved"] += 1
            print(f"  saved {name}")
    return tally
