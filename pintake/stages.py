"""Pipeline stages.

Each function here is the actual work for one stage, taking explicit arguments
rather than a parsed-argument blob, so it can be called from the CLI, a REPL or
a test. `cli.py` is a thin click layer over this module.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from . import classify as clf
from . import convert, db, extract, normalize, paperless
from .config import ARCHIVE_DIR, DOC_EXTS, IMG_EXTS
from .http import get_json

DISPOSITIONS = ("file", "review", "skip")

SKIP_DIRS = {
    "node_modules", ".git", "__MACOSX", "Library", ".Trash", "venv", ".venv",
    "site-packages", "dist-info",
    "books",  # ledger/beancount/sqlite live here — not documents
    ARCHIVE_DIR.name,  # already filed; re-scanning it would be pure churn
}
SKIP_NAME_PREFIXES = ("~$", "._")


# ---------------------------------------------------------------- helpers

def iso(ts) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def md5(path, chunk=1 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def eligible(path: Path, exts) -> bool:
    if path.name.startswith(SKIP_NAME_PREFIXES) or path.name.startswith("."):
        return False
    if path.suffix.lower() not in exts:
        return False
    try:
        return path.is_file() and path.stat().st_size > 1024
    except OSError:
        return False


def _text_fingerprint(text: str) -> str:
    """Whitespace/punctuation-normalised text, numbers preserved."""
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _unique_dest(dest: Path) -> Path:
    """Never overwrite: foo.pdf -> foo (2).pdf -> foo (3).pdf ..."""
    if not dest.exists():
        return dest
    for i in range(2, 1000):
        cand = dest.with_name(f"{dest.stem} ({i}){dest.suffix}")
        if not cand.exists():
            return cand
    raise RuntimeError(f"cannot find a free name for {dest}")


# ---------------------------------------------------------------- stages

def scan(directory: str, depth: int = 2, images: bool = False) -> None:
    con = db.connect()
    exts = set(DOC_EXTS) | (IMG_EXTS if images else set())
    root = Path(directory).expanduser().resolve()

    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")
                       and not d.endswith((".app", ".framework"))]
        if len(Path(dirpath).relative_to(root).parts) > depth:
            dirnames[:] = []
            continue
        for fn in filenames:
            p = Path(dirpath) / fn
            if eligible(p, exts):
                found.append(p)

    known = {r[0] for r in con.execute("SELECT path FROM files")}
    added = 0
    for p in found:
        sp = str(p)
        if sp in known:
            continue
        st = p.stat()
        con.execute(
            "INSERT INTO files(path,name,ext,size,mtime,md5,status,updated) "
            "VALUES (?,?,?,?,?,?,'new',?)",
            (sp, p.name, p.suffix.lower(), st.st_size, st.st_mtime,
             md5(p), time.time()),
        )
        added += 1
        if added % 50 == 0:
            con.commit()
            print(f"  hashed {added}...", file=sys.stderr)
    con.commit()
    print(f"scan: {len(found)} eligible under {root}, {added} newly recorded, "
          f"{len(found) - added} already known")


def sync() -> None:
    con = db.connect()
    n = paperless.sync_checksums(con)
    print(f"sync: {n} documents already in Paperless")

    cur = con.execute("""
        UPDATE files SET status='dupe_paperless',
            dupe_of=(SELECT 'paperless:' || doc_id FROM paperless_docs p
                     WHERE p.checksum=files.md5),
            updated=strftime('%s','now')
        WHERE status IN ('new','extracted','classified')
          AND md5 IN (SELECT checksum FROM paperless_docs)
    """)
    con.commit()
    print(f"      {cur.rowcount} local files already in Paperless (exact match)")

    # Intra-corpus duplicates: keep the shortest path per md5.
    rows = con.execute("""
        SELECT md5, path FROM files
        WHERE status NOT IN ('dupe_paperless','dupe_local')
        ORDER BY md5, length(path), path
    """).fetchall()
    seen, dl = {}, 0
    for r in rows:
        if r["md5"] in seen:
            db.touch(con, r["path"], status="dupe_local", dupe_of=seen[r["md5"]])
            dl += 1
        else:
            seen[r["md5"]] = r["path"]
    print(f"      {dl} duplicate copies within the scan set")


def _extract_one(row):
    try:
        return row["path"], extract.extract(row["path"]), None
    except Exception as e:  # noqa: BLE001
        return row["path"], None, f"{type(e).__name__}: {e}"


def extract_text(workers: int = 6, limit: int = 100_000) -> None:
    con = db.connect()
    rows = con.execute(
        "SELECT * FROM files WHERE status='new' ORDER BY size LIMIT ?", (limit,),
    ).fetchall()
    print(f"extract: {len(rows)} files, {workers} workers")
    done = 0
    with cf.ThreadPoolExecutor(workers) as ex:
        for path, res, err in ex.map(_extract_one, rows):
            done += 1
            if err:
                db.touch(con, path, status="error", error=err)
            else:
                db.touch(con, path, status="extracted", text=res["text"],
                         pages=res["pages"], ocr=res["ocr"],
                         text_chars=res["chars"], error=None)
            if done % 25 == 0 or done == len(rows):
                print(f"  {done}/{len(rows)}", file=sys.stderr)


VISION_TEXT_THRESHOLD = 120  # below this, the text model is guessing
SERVER_DOWN_AFTER = 5        # this many server-side failures in a row stops the run instead of failing every file


def _classify_one(tax, row, model, vision=True):
    try:
        text = row["text"] or ""
        img = None
        if vision and len(text.strip()) < VISION_TEXT_THRESHOLD:
            # No usable text: show the model the page rather than let it guess
            # from the filename.
            img = extract.page_image(row["path"])
        if img:
            meta = clf.classify_image(
                tax, filename=row["name"], mtime_iso=iso(row["mtime"]),
                image_png=img, model=model,
            )
        else:
            meta = clf.classify(
                tax, filename=row["name"], mtime_iso=iso(row["mtime"]),
                pages=row["pages"], ocr=row["ocr"], text=text, model=model,
            )
        meta = normalize.normalize(meta, tax, text=row["text"] or "")
        meta = normalize.apply_series(meta, row["path"])
        return row["path"], meta, None
    except Exception as e:  # noqa: BLE001
        return row["path"], None, f"{type(e).__name__}: {e}"


def classify(workers: int = 3, limit: int = 100_000, model: str | None = None,
             vision: bool = True) -> None:
    con = db.connect()
    tax = paperless.taxonomy()
    rows = con.execute(
        "SELECT * FROM files WHERE status='extracted' ORDER BY mtime DESC LIMIT ?",
        (limit,),
    ).fetchall()
    if not rows:
        print("classify: nothing waiting")
        return
    using = clf.preflight(model)                 # raises LLMUnavailable with what to do about it
    print(f"classify: {len(rows)} files, model={' / '.join(using)}, "
          f"{workers} workers")
    t0, done, down, deferred = time.time(), 0, 0, 0
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(_classify_one, tax, r, model, vision) for r in rows]
        for fut in cf.as_completed(futs):
            if fut.cancelled():
                continue
            path, meta, err = fut.result()
            done += 1
            if err and clf.infrastructure_error(err):
                # The server's problem, not the document's: the row stays 'extracted' and the next run retries it.
                down, deferred = down + 1, deferred + 1
                if down >= SERVER_DOWN_AFTER:
                    for f in futs:
                        f.cancel()
                    raise clf.LLMUnavailable(
                        f"the model server failed {down} requests in a row ({err[:200]}). Stopped after {done} of "
                        f"{len(rows)}; nothing was marked as failed. Re-run classify when it is back.")
            elif err:
                down = 0
                db.touch(con, path, status="error", error=err)
            else:
                down = 0
                db.touch(con, path, status="classified",
                         meta_json=json.dumps(meta),
                         decision=meta["disposition"], error=None)
            rate = done / max(time.time() - t0, 1e-9)
            print(f"  {done}/{len(rows)}  {rate * 60:5.1f}/min  "
                  f"{(meta or {}).get('disposition', 'RETRY' if err and clf.infrastructure_error(err) else 'ERR'):6} "
                  f"{Path(path).name[:60]}" + (f"  ({err[:80]})" if err else ""), file=sys.stderr)
    if deferred:
        print(f"classify: {deferred} left for the next run (the model server failed on them)")


def counts() -> dict:
    """{status: n} plus {"decision:<x>": n} for classified rows: what `go` prints between stages."""
    con = db.connect()
    out = {r["status"]: r["n"] for r in con.execute("SELECT status, count(*) n FROM files GROUP BY status")}
    for r in con.execute("SELECT coalesce(override, decision) d, count(*) n FROM files WHERE status='classified' GROUP BY 1"):
        out[f"decision:{r['d']}"] = r["n"]
    return out


def report(disposition: str = "file", limit: int = 500) -> None:
    con = db.connect()
    print("== status")
    for r in con.execute("SELECT status, count(*) n, sum(size)/1048576.0 mb "
                         "FROM files GROUP BY status ORDER BY n DESC"):
        print(f"  {r['status']:16} {r['n']:5d}  {r['mb']:8.1f} MB")

    rows = con.execute(
        "SELECT * FROM files WHERE status='classified' "
        "AND coalesce(override, decision)=? ORDER BY mtime DESC",
        (disposition,),
    ).fetchall()
    print(f"\n== {disposition}  ({len(rows)})")
    print(f"{'DATE':10} {'CONF':4} {'TYPE':22} {'CORRESPONDENT':26} "
          f"{'TITLE':46} TAGS")
    for r in rows[:limit]:
        m = json.loads(r["meta_json"])
        print(f"{(m['created'] or '-'):10} {m['confidence']:.2f} "
              f"{(m['document_type'] or '-')[:22]:22} "
              f"{(m['correspondent'] or '-')[:26]:26} {m['title'][:46]:46} "
              f"{','.join(m['tags'])}")


def neardupe(min_chars: int = 200, verbose: bool = False) -> None:
    """Flag the same document saved under a different filename.

    Sameness is EXACT equality of the normalised text, never a similarity
    threshold. Batches of financial confirmations are ~99% identical boilerplate
    differing only in a confirmation number, so any fuzzy cutoff loose enough to
    catch real duplicates also collapses distinct transactions and silently drops
    records. Exact equality still catches the real target: one document saved
    twice, byte-different only in PDF metadata, which MD5 misses.
    """
    con = db.connect()
    rows = con.execute(
        "SELECT path, name, text, text_chars FROM files "
        "WHERE status='classified' AND coalesce(override, decision)='file'"
    ).fetchall()

    seen, flagged = {}, 0
    for r in sorted(rows, key=lambda r: (-(r["text_chars"] or 0), len(r["path"]))):
        fp = _text_fingerprint(r["text"])
        if len(fp) < min_chars:
            continue  # too little text to judge; leave it alone
        if fp in seen:
            db.touch(con, r["path"], status="dupe_local", dupe_of=seen[fp][0])
            flagged += 1
            if verbose:
                print(f"  dupe  {r['name'][:56]}\n     of {seen[fp][1][:56]}")
        else:
            seen[fp] = (r["path"], r["name"])
    print(f"neardupe: {flagged} exact-text duplicates flagged "
          f"across {len(rows)} documents")


def archive(dest: str, source: str, include_duplicates: bool = False,
            dry_run: bool = False) -> None:
    """Move filed originals out of the scanned tree, preserving structure.

    A file is only moved once its document is confirmed to exist in Paperless
    right now — not merely uploaded, not merely consumed. The state db is
    rewritten to the new location so every other stage keeps working.
    """
    con = db.connect()
    dest_root = Path(dest).expanduser().resolve()
    src_root = Path(source).expanduser().resolve()

    statuses = ["uploaded"]
    if include_duplicates:
        statuses += ["dupe_local", "dupe_paperless"]
    rows = con.execute(
        "SELECT path, name, status, paperless_id FROM files "
        f"WHERE status IN ({','.join('?' * len(statuses))})", statuses,
    ).fetchall()

    live = set()
    url = f"{paperless.PAPERLESS_URL}/api/documents/?page_size=1000&fields=id"
    while url:
        d = get_json(url, paperless.PAPERLESS_HEADERS)
        live.update(r["id"] for r in d["results"])
        url = d.get("next")
    print(f"archive: {len(live)} documents live in Paperless")

    moved = skipped = missing = already = consuming = 0
    for r in rows:
        src = Path(r["path"])
        # Idempotency: a second run must not re-archive what it already moved.
        # Without this the path relative to --source now *starts* with the
        # archive directory, so the file gets buried one level deeper each run.
        if dest_root == src.parent or dest_root in src.parents:
            already += 1
            continue
        if r["status"] == "uploaded" and r["paperless_id"] is None:
            consuming += 1          # uploaded, but Paperless has not confirmed a document for it yet
            continue
        if r["status"] == "uploaded" and r["paperless_id"] not in live:
            print(f"  SKIP (document {r['paperless_id']} is gone from Paperless) {r['name'][:60]}")
            missing += 1
            continue
        if not src.exists():
            skipped += 1
            continue
        try:
            rel = src.relative_to(src_root)
        except ValueError:
            rel = Path(src.name)
        sub = "duplicates" if r["status"].startswith("dupe") else ""
        target = _unique_dest(dest_root / sub / rel)
        if dry_run:
            print(f"  {src} -> {target}")
            moved += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(target))
        con.execute("UPDATE files SET path=?, updated=? WHERE path=?",
                    (str(target), time.time(), str(src)))
        moved += 1
        if moved % 100 == 0:
            con.commit()
            print(f"  moved {moved}...", file=sys.stderr)
    con.commit()
    print(f"archive: {moved} moved{' (dry run)' if dry_run else ''}, "
          f"{already} already archived, {skipped} already gone, "
          f"{missing} missing from Paperless"
          + (f"\n         {consuming} left in place: Paperless is still consuming them. "
             "Run `pintake finalize` until none are pending, then archive again." if consuming else ""))


# Filenames that carry no information — the scanner's default output, phone
# camera dumps, "Untitled". These are what `rename` targets by default; a name
# someone actually chose is left alone.
GENERIC_NAME_RE = re.compile(
    r"^(scan|scanned[-_ ]?document|document|doc|untitled|image|img|photo|"
    r"file|new[-_ ]?doc\w*|adobe[-_ ]?scan)[-_ ]?\d*(\s*\(\d+\))?$", re.I)

SLUG_STOPWORDS = {"a", "an", "the", "of", "for", "and", "to", "at", "in", "on",
                  "with", "from", "by"}


def slugify(title: str, max_len: int = 72) -> str:
    """kebab-case slug in the house style: lowercase, no filler words."""
    words = re.findall(r"[A-Za-z0-9]+", title.lower())
    kept = [w for w in words if w not in SLUG_STOPWORDS] or words
    slug = ""
    for w in kept:
        cand = f"{slug}-{w}" if slug else w
        if len(cand) > max_len:
            break
        slug = cand
    return slug or "document"


def proposed_name(meta: dict, ext: str) -> str:
    """<slug>-<YYYYMMDD><ext>, matching the existing scans/ naming."""
    slug = slugify(meta.get("title") or "document")
    created = (meta.get("created") or "").replace("-", "")
    return f"{slug}-{created}{ext}" if created else f"{slug}{ext}"


def rename(directory: str | None = None, all_files: bool = False,
           dry_run: bool = False) -> None:
    """Give scanner-default filenames a name derived from their content.

    Only files whose current name carries no information (scan0017.pdf,
    IMG_4821.pdf, Untitled.pdf) are touched unless --all is passed, so names
    someone deliberately chose are preserved.
    """
    con = db.connect()
    sql = ("SELECT path, name, ext, meta_json FROM files "
           "WHERE status='classified' AND meta_json IS NOT NULL")
    params: list = []
    if directory:
        sql += " AND path LIKE ?"
        params.append(str(Path(directory).expanduser().resolve()) + "%")
    rows = con.execute(sql, params).fetchall()

    renamed = kept = 0
    for r in rows:
        src = Path(r["path"])
        if not all_files and not GENERIC_NAME_RE.match(src.stem):
            kept += 1
            continue
        meta = json.loads(r["meta_json"])
        new_name = proposed_name(meta, src.suffix.lower())
        if new_name == src.name:
            kept += 1
            continue
        if not src.exists():
            continue
        dest = _unique_dest(src.with_name(new_name))
        print(f"  {src.name}  ->  {dest.name}")
        if dry_run:
            renamed += 1
            continue
        src.rename(dest)
        con.execute("UPDATE files SET path=?, name=?, updated=? WHERE path=?",
                    (str(dest), dest.name, time.time(), str(src)))
        renamed += 1
    con.commit()
    print(f"rename: {renamed} renamed{' (dry run)' if dry_run else ''}, "
          f"{kept} left alone")


def triage_errors() -> None:
    """Sort consume failures into duplicates, transient faults and real ones."""
    con = db.connect()
    rows = con.execute("SELECT path, name, error FROM files "
                       "WHERE status='error'").fetchall()
    dupes = transient = repair = other = 0
    for r in rows:
        err = r["error"] or ""
        if "is a duplicate of" in err:
            # Paperless' own checksum backstop: already filed under another name.
            db.touch(con, r["path"], status="dupe_paperless", error=None)
            dupes += 1
        elif "database is locked" in err or "Connection reset" in err:
            db.touch(con, r["path"], status="classified", error=None)
            transient += 1
        elif "Ghostscript" in err or "rendering failed" in err:
            db.touch(con, r["path"], status="classified", error=None)
            repair += 1
        else:
            other += 1
            print(f"  unhandled: {r['name'][:56]}: {err[:120]}")
    print(f"triage: {dupes} already-filed duplicates, {transient} transient "
          f"(retry as-is), {repair} need --repair-pdf, {other} unhandled")


def _cf_payload(cf_by_name: dict, values: dict) -> list:
    """Turn {field name: value} into Paperless' custom_fields payload.

    A `select` field wants the *id* of the chosen option, not its label, so
    labels are resolved against the field definition and dropped if unknown —
    an unmatched label would otherwise 400 the whole document.
    """
    out = []
    for name, value in values.items():
        field = cf_by_name.get(name)
        if field is None or value in (None, ""):
            continue
        if field["data_type"] == "select":
            opts = (field.get("extra_data") or {}).get("select_options") or []
            match = next((o for o in opts
                          if str(o.get("label", "")).lower() == str(value).lower()),
                         None)
            if match is None:
                continue
            value = match.get("id", match.get("label"))
        out.append({"field": field["id"], "value": value})
    return out


def finalize() -> dict:
    """Resolve consume tasks to document ids and set custom field values. Returns
    {"done", "pending", "failed"}: Paperless consumes in its own time (OCR, a queue), so right after
    an upload most documents are still pending and a later run picks them up.

    `post_document` can only attach custom fields by pk, never with a value, so
    every derived field is written here once the consumer has produced a real
    document.
    """
    con = db.connect()
    tax = paperless.taxonomy()
    cf_by_name = {c["name"]: c for c in tax["custom_fields"]}
    rows = con.execute("SELECT path, task_id, meta_json FROM files "
                       "WHERE status='uploaded' AND task_id IS NOT NULL "
                       "AND paperless_id IS NULL").fetchall()
    print(f"finalize: {len(rows)} uploads awaiting confirmation")

    done = pending = failed = patched = 0
    for r in rows:
        try:
            t = paperless.task_status(r["task_id"])
        except Exception as e:  # noqa: BLE001
            print(f"  task lookup failed: {e}")
            continue
        if not t or t.get("status") not in ("SUCCESS", "FAILURE"):
            pending += 1
            continue
        if t.get("status") == "FAILURE":
            failed += 1
            db.touch(con, r["path"], status="error",
                     error=f"consume: {str(t.get('result'))[:600]}")
            continue
        doc_id = t.get("related_document")
        if not doc_id:
            pending += 1
            continue
        done += 1
        db.touch(con, r["path"], paperless_id=int(doc_id))

        m = json.loads(r["meta_json"])
        values = normalize.derive_custom_fields(r["path"], m)
        payload = _cf_payload(cf_by_name, values)
        if payload:
            try:
                paperless.patch_document(int(doc_id),
                                         {"custom_fields": payload})
                patched += 1
            except Exception as e:  # noqa: BLE001
                print(f"  custom-field patch failed for {doc_id}: {e}")
    print(f"  confirmed {done}, custom fields set on {patched}, "
          f"still consuming {pending}, consume failures {failed}")
    return {"done": done, "pending": pending, "failed": failed}


def finalize_wait(minutes: float = 45, every_s: float = 30, sleep=time.sleep) -> dict:
    """finalize, repeated until Paperless has consumed everything that was uploaded (or `minutes` pass).
    Stops early if a whole ten minutes go by with nothing new confirmed: the consumer is stuck, not slow."""
    t0, last_progress, total = time.time(), time.time(), {"done": 0, "pending": 0, "failed": 0}
    while True:
        r = finalize()
        total["done"] += r["done"]; total["failed"] += r["failed"]; total["pending"] = r["pending"]
        if r["done"] or r["failed"]:
            last_progress = time.time()
        if not r["pending"]:
            return total
        if time.time() - t0 > minutes * 60:
            print(f"  gave up waiting after {minutes:.0f} min with {r['pending']} still consuming")
            return total
        if time.time() - last_progress > 600:
            print(f"  nothing consumed in 10 min with {r['pending']} waiting: check Paperless's consumer")
            return total
        sleep(every_s)


def backfill_fields(dry_run: bool = False, limit: int = 100_000) -> None:
    """Re-derive custom field values for documents already in Paperless.

    `finalize` only runs once per upload, so a field added (or a derivation
    rule changed) afterwards needs this to reach documents already filed.
    """
    con = db.connect()
    tax = paperless.taxonomy()
    cf_by_name = {c["name"]: c for c in tax["custom_fields"]}
    rows = con.execute(
        "SELECT path, paperless_id, meta_json FROM files "
        "WHERE paperless_id IS NOT NULL AND meta_json IS NOT NULL LIMIT ?",
        (limit,),
    ).fetchall()

    patched = skipped = failed = 0
    for r in rows:
        values = normalize.derive_custom_fields(
            r["path"], json.loads(r["meta_json"]))
        payload = _cf_payload(cf_by_name, values)
        if not payload:
            skipped += 1
            continue
        if dry_run:
            if patched < 12:
                print(f"  #{r['paperless_id']:5} {Path(r['path']).name[:52]:52} "
                      f"{values}")
            patched += 1
            continue
        try:
            paperless.patch_document(int(r["paperless_id"]),
                                     {"custom_fields": payload})
            patched += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            if failed < 5:
                print(f"  fail #{r['paperless_id']}: {str(e)[:140]}")
        if patched % 100 == 0 and not dry_run:
            print(f"  {patched} patched...", file=sys.stderr)
    print(f"backfill-fields: {patched} patched{' (dry run)' if dry_run else ''}, "
          f"{skipped} had nothing derivable, {failed} failed")


def consolidate(dry_run: bool = False) -> None:
    """Fold twin spellings among newly-proposed names into one canonical each."""
    con = db.connect()
    tax = paperless.taxonomy()
    rows = con.execute("SELECT path, meta_json FROM files WHERE status='classified' "
                       "AND coalesce(override, decision)='file'").fetchall()
    metas = [(r["path"], json.loads(r["meta_json"])) for r in rows]

    plans = {}
    for field, key in (("correspondent", "correspondents"),
                       ("document_type", "document_types")):
        counts = {}
        for _p, m in metas:
            if m.get(field):
                counts[m[field]] = counts.get(m[field], 0) + 1
        mapping = normalize.consolidate(counts, [i["name"] for i in tax[key]])
        plans[field] = mapping
        if mapping:
            print(f"== {field}")
            for src, dst in sorted(mapping.items(), key=lambda x: -counts[x[0]]):
                print(f"   {counts[src]:4d}  {src}  ->  {dst}")

    if dry_run:
        print("\n(dry run — nothing written)")
        return
    n = 0
    for path, m in metas:
        new = dict(m)
        for field, mapping in plans.items():
            if new.get(field) in mapping:
                new[field] = mapping[new[field]]
        if new != m:
            db.touch(con, path, meta_json=json.dumps(new))
            n += 1
    print(f"\nconsolidate: {n} documents rewritten")


def renorm(verbose: bool = False) -> None:
    """Re-apply normalisation to stored results (after editing normalize.py).

    Cheaper than re-classifying: the model output is already on disk, only the
    taxonomy-snapping rules changed.
    """
    con = db.connect()
    tax = paperless.taxonomy()
    rows = con.execute("SELECT path, text, meta_json FROM files "
                       "WHERE status='classified'").fetchall()
    n = 0
    for r in rows:
        meta = json.loads(r["meta_json"])
        new = normalize.apply_series(
            normalize.normalize(meta, tax, text=r["text"] or ""), r["path"])
        if new != meta:
            db.touch(con, r["path"], meta_json=json.dumps(new))
            n += 1
            if verbose:
                for k in ("correspondent", "document_type", "tags"):
                    if new.get(k) != meta.get(k):
                        print(f"  {Path(r['path']).name[:44]}: {k} "
                              f"{meta.get(k)!r} -> {new.get(k)!r}")
    print(f"renorm: {n}/{len(rows)} results changed")


def retag() -> None:
    """Re-apply path-based series rules to rows already classified."""
    con = db.connect()
    rows = con.execute("SELECT path, meta_json FROM files "
                       "WHERE status='classified'").fetchall()
    n = 0
    for r in rows:
        meta = json.loads(r["meta_json"])
        new = normalize.apply_series(meta, r["path"])
        if new != meta:
            db.touch(con, r["path"], meta_json=json.dumps(new))
            n += 1
    print(f"retag: {n}/{len(rows)} rows tagged as bulk series")


def taxonomy(verbose: bool = False) -> None:
    """Existing Paperless taxonomy vs. what the classifier wants to add."""
    con = db.connect()
    tax = paperless.taxonomy()
    existing = {k: paperless.name_map(v) for k, v in tax.items()}
    counts = {k: {i["name"]: i.get("document_count", 0) for i in v}
              for k, v in tax.items()}

    proposed = {"tags": {}, "correspondents": {}, "document_types": {}}
    rows = con.execute(
        "SELECT meta_json FROM files WHERE status='classified' "
        "AND coalesce(override, decision)='file'"
    ).fetchall()
    for r in rows:
        m = json.loads(r["meta_json"])
        names = [("tags", t) for t in m["tags"]]
        if m["correspondent"]:
            names.append(("correspondents", m["correspondent"]))
        if m["document_type"]:
            names.append(("document_types", m["document_type"]))
        for kind, name in names:
            proposed[kind][name] = proposed[kind].get(name, 0) + 1

    for kind in ("tags", "correspondents", "document_types"):
        reuse = {n: c for n, c in proposed[kind].items()
                 if n.strip().lower() in existing[kind]}
        new = {n: c for n, c in proposed[kind].items()
               if n.strip().lower() not in existing[kind]}
        print(f"== {kind}: {len(existing[kind])} existing | "
              f"reused {len(reuse)} | NEW {len(new)}")
        for n, c in sorted(new.items(), key=lambda x: -x[1]):
            flag = "  <-- 1 doc only" if c == 1 else ""
            print(f"   NEW  {c:4d}  {n}{flag}")
        if verbose:
            for n, c in sorted(reuse.items(), key=lambda x: -x[1]):
                print(f"   use  {c:4d}  {n}  (had {counts[kind].get(n, 0)})")
        print()


def show(pattern: str, text: int = 0) -> None:
    con = db.connect()
    r = con.execute("SELECT * FROM files WHERE path LIKE ? LIMIT 1",
                    (f"%{pattern}%",)).fetchone()
    if not r:
        raise LookupError(f"no file matching {pattern!r}")
    print(f"path   : {r['path']}\nmd5    : {r['md5']}\nstatus : {r['status']}\n"
          f"pages  : {r['pages']}  chars={r['text_chars']} ocr={r['ocr']}")
    if r["meta_json"]:
        print("meta   :", json.dumps(json.loads(r["meta_json"]), indent=2))
    if text:
        print("-" * 70)
        print((r["text"] or "")[:text])


def set_disposition(to: str, patterns: tuple[str, ...]) -> None:
    con = db.connect()
    n = 0
    for pat in patterns:
        cur = con.execute(
            "UPDATE files SET override=?, updated=strftime('%s','now') "
            "WHERE path LIKE ?", (to, f"%{pat}%"))
        n += cur.rowcount
    con.commit()
    print(f"set {n} rows -> {to}")


def upload(limit: int = 100_000, dry_run: bool = False,
           repair_pdf: bool = False) -> None:
    con = db.connect()
    tax = paperless.taxonomy()
    caches = {k: paperless.name_map(v) for k, v in tax.items()}

    rows = con.execute(
        "SELECT * FROM files WHERE status='classified' "
        "AND coalesce(override, decision)='file' ORDER BY mtime DESC LIMIT ?",
        (limit,),
    ).fetchall()
    print(f"upload: {len(rows)} documents "
          f"({'DRY RUN' if dry_run else 'LIVE'}) -> {paperless.PAPERLESS_URL}")

    for i, r in enumerate(rows, 1):
        m = json.loads(r["meta_json"])
        tag_names = list(m["tags"])

        if dry_run:
            print(f"  [{i}/{len(rows)}] {m['title']}  <- {r['name']}")
            print(f"        type={m['document_type']} corr={m['correspondent']} "
                  f"date={m['created']} tags={tag_names}")
            continue

        try:
            tag_ids = [paperless.ensure("tags", t, caches["tags"])
                       for t in tag_names]
            corr = (paperless.ensure("correspondents", m["correspondent"],
                                     caches["correspondents"])
                    if m["correspondent"] else None)
            dtype = (paperless.ensure("document_types", m["document_type"],
                                      caches["document_types"])
                     if m["document_type"] else None)
            # Paperless rejects Word documents without Tika; hand it a PDF.
            src = convert.to_pdf(r["path"])
            if repair_pdf and Path(src).suffix.lower() == ".pdf":
                src = convert.repair_pdf(src)
            task = paperless.upload(
                str(src), title=m["title"], created=m["created"],
                correspondent=corr, document_type=dtype, tags=tag_ids,
            )
            db.touch(con, r["path"], status="uploaded", task_id=task, error=None)
            print(f"  [{i}/{len(rows)}] OK   {m['title'][:60]}  task={task[:8]}")
        except Exception as e:  # noqa: BLE001
            db.touch(con, r["path"], status="error", error=f"upload: {e}")
            print(f"  [{i}/{len(rows)}] FAIL {r['name'][:60]}: {e}")
