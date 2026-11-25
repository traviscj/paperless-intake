"""pintake — file ~/Downloads into Paperless-ngx, with a local model doing the
reading and a normalisation layer keeping the archive's own vocabulary
authoritative.

\b
Pipeline (every stage is resumable; state lives in state.db):
  scan       walk a directory and MD5-hash candidate files
  sync       pull Paperless' checksums, flag duplicates both ways
  extract    pull text out of each file, OCR only where needed
  classify   local model -> structured metadata, snapped to the taxonomy
  neardupe   flag the same document saved under a second filename
  consolidate  fold twin spellings of newly-proposed names
  taxonomy   show what new tags/correspondents would be created
  report     dense triage table
  upload     POST to Paperless
  finalize   confirm consumption, set custom field values
  archive    move filed originals out of the scanned tree

Run stages in that order; each only touches rows left in the matching state,
so re-running one is safe and picks up where it stopped.
"""
from __future__ import annotations

from pathlib import Path

import click

from . import mailfetch, stages
from .config import (ARCHIVE_DIR, DB_PATH, LLM_MODEL, LLM_URL, PAPERLESS_URL)
from .stages import DISPOSITIONS

CONTEXT_SETTINGS = {
    "help_option_names": ["-h", "--help"],
    "max_content_width": 100,
}

workers_option = click.option(
    "--workers", type=click.IntRange(1, 64), default=6, show_default=True,
    help="Parallel workers.")
limit_option = click.option(
    "--limit", type=int, default=100_000, show_default="all",
    help="Process at most this many rows (useful for a trial run).")
verbose_option = click.option(
    "-v", "--verbose", is_flag=True, help="Show per-item detail.")
dry_run_option = click.option(
    "--dry-run", is_flag=True, help="Show what would happen; write nothing.")


@click.group(context_settings=CONTEXT_SETTINGS, help=__doc__)
@click.version_option(package_name="pintake")
def main() -> None:
    pass


@main.command()
@click.argument("directory", default="~/Downloads")
@click.option("--depth", type=int, default=2, show_default=True,
              help="How many directory levels below DIRECTORY to descend.")
@click.option("--images/--no-images", default=False, show_default=True,
              help="Also consider png/jpg/tiff, not just documents.")
def scan(directory: str, depth: int, images: bool) -> None:
    """Record and MD5-hash candidate files under DIRECTORY.

    Files already known are left alone, so re-scanning after new downloads
    only adds the new ones.
    """
    stages.scan(directory, depth=depth, images=images)


@main.command()
def sync() -> None:
    """Pull Paperless' checksums and flag duplicates in both directions.

    Paperless stores an MD5 of every original but does not expose it over REST,
    so this reads it out of the container's ORM over SSH. Marks local files
    already filed (dupe_paperless) and redundant copies within the scan set
    (dupe_local).
    """
    stages.sync()


@main.command("extract")
@workers_option
@limit_option
def extract_cmd(workers: int, limit: int) -> None:
    """Pull text out of each scanned file.

    Uses the PDF text layer where there is one; falls back to rendering pages
    and OCRing them only when there is not.
    """
    stages.extract_text(workers=workers, limit=limit)


@main.command("classify")
@click.option("--workers", type=click.IntRange(1, 64), default=3,
              show_default=True, help="Parallel requests to the model server.")
@limit_option
@click.option("--model", default=None,
              help=f"Model name on the llama.cpp server  [default: {LLM_MODEL}]")
@click.option("--vision/--no-vision", default=True, show_default=True,
              help="For files with no extractable text, render the first page "
                   "and let the vision model read it instead of guessing.")
def classify_cmd(workers: int, limit: int, model: str | None,
                 vision: bool) -> None:
    """Ask the local model to read each document and describe it.

    Every prompt carries the live tag/correspondent/type lists, so the model
    reuses existing names; the result is then snapped onto the archive's
    conventions by normalize.py before it is stored.
    """
    from .classify import LLMUnavailable
    try:
        stages.classify(workers=workers, limit=limit, model=model, vision=vision)
    except LLMUnavailable as e:
        raise click.ClickException(str(e)) from e


@main.command()
@click.option("--min-chars", type=int, default=200, show_default=True,
              help="Skip files with less normalised text than this.")
@verbose_option
def neardupe(min_chars: int, verbose: bool) -> None:
    """Flag the same document saved under a different filename.

    Matches on EXACT normalised-text equality, never a similarity score:
    financial confirmations are ~99% identical boilerplate differing only in a
    confirmation number, so any fuzzy cutoff that catches real duplicates also
    collapses distinct transactions.
    """
    stages.neardupe(min_chars=min_chars, verbose=verbose)


@main.command()
@click.argument("directory", default="~/Downloads")
@click.option("--file", "do_file", is_flag=True,
              help="After the review step: upload everything marked 'file', confirm it was consumed, archive the originals.")
@click.option("--model", default=None, help=f"Model for classify  [default: {LLM_MODEL}]")
@click.option("--workers", type=click.IntRange(1, 64), default=2, show_default=True, help="Parallel requests to the model server.")
def go(directory: str, do_file: bool, model: str | None, workers: int) -> None:
    """The whole intake in one command, in two steps with a look in between.

    \b
    pintake go            scan, sync, extract, classify, neardupe, consolidate, then a summary and stop
    pintake report ...    look at what it decided; `pintake set --to skip|file PATTERN` to overrule
    pintake go --file     upload what is marked 'file', finalize, archive the originals

    Before anything is touched it checks that Paperless answers and that the model can actually load, so a
    missing model stops the run with a message instead of failing every file. Every stage only touches rows in
    its own state, so after any failure just run the same command again.
    """
    from . import classify as clf
    from . import paperless

    def step(n: int, total: int, name: str, fn) -> None:
        click.secho(f"── {n}/{total} {name} ──", fg="cyan", err=True)
        try:
            fn()
        except clf.LLMUnavailable as e:
            raise click.ClickException(f"{name}: {e}") from e
        except Exception as e:  # noqa: BLE001
            raise click.ClickException(f"{name} failed: {type(e).__name__}: {e}\nFix that and re-run `pintake go{' --file' if do_file else ''}`; finished stages are skipped.") from e

    def summary() -> None:
        c = stages.counts()
        click.echo("  ".join(f"{k} {v}" for k, v in sorted(c.items())))

    try:
        paperless.taxonomy()
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(f"Paperless is not answering ({PAPERLESS_URL}): {e}") from e
    if not do_file:
        try:
            click.echo("model: " + " / ".join(clf.preflight(model)) + " loads")
        except clf.LLMUnavailable as e:
            raise click.ClickException(str(e)) from e
        step(1, 6, f"scan {directory}", lambda: stages.scan(directory))
        step(2, 6, "sync", stages.sync)
        step(3, 6, "extract", stages.extract_text)
        step(4, 6, "classify", lambda: stages.classify(workers=workers, model=model))
        step(5, 6, "neardupe", stages.neardupe)
        step(6, 6, "consolidate", stages.consolidate)
        summary()
        c = stages.counts()
        click.echo(f"\nreview: `pintake report` ({c.get('decision:file', 0)} to file, {c.get('decision:review', 0)} to look at, "
                   f"{c.get('decision:skip', 0)} skipped, {c.get('error', 0)} errors), `pintake taxonomy` for new names, then `pintake go --file`.")
        return
    step(1, 3, "upload", stages.upload)
    step(2, 3, "finalize", stages.finalize)
    step(3, 3, "archive", lambda: stages.archive(dest=str(ARCHIVE_DIR), source=str(Path(directory).expanduser())))
    summary()


@main.command("backfill-fields")
@dry_run_option
@limit_option
def backfill_fields(dry_run: bool, limit: int) -> None:
    """Re-derive custom fields for documents already filed in Paperless.

    Run after adding a custom field or changing a derivation rule; `finalize`
    only fires once per upload and will not revisit them.
    """
    stages.backfill_fields(dry_run=dry_run, limit=limit)


@main.command()
@dry_run_option
def consolidate(dry_run: bool) -> None:
    """Fold twin spellings of newly-proposed names into one canonical each.

    Classification snaps each name against what Paperless already holds, but two
    names that are both new never meet — "CKO Real Estate" and "CKO Real Estate
    LLC" would create twin correspondents. Here the most frequent spelling wins.
    """
    stages.consolidate(dry_run=dry_run)


@main.command()
@verbose_option
def taxonomy(verbose: bool) -> None:
    """Show which tags/correspondents/types are reused vs. newly invented.

    Run this before uploading. Anything marked "1 doc only" is usually drift
    worth folding into an existing name via the alias tables in normalize.py.
    """
    stages.taxonomy(verbose=verbose)


@main.command()
@click.option("--disposition", type=click.Choice(DISPOSITIONS), default="file",
              show_default=True, help="Which pile to list.")
@click.option("--limit", type=int, default=500, show_default=True,
              help="Maximum rows to print.")
def report(disposition: str, limit: int) -> None:
    """Print the state summary and a dense table of one disposition."""
    stages.report(disposition=disposition, limit=limit)


@main.command("upload")
@limit_option
@dry_run_option
@click.option("--repair-pdf", is_flag=True,
              help="Re-render PDFs through poppler first; use when Paperless "
                   "reports 'Ghostscript PDF/A rendering failed'.")
def upload_cmd(limit: int, dry_run: bool, repair_pdf: bool) -> None:
    """POST every document marked 'file' to Paperless.

    Tags, correspondents and types are created on demand, matched
    case-insensitively so nothing duplicates. Word documents are converted to
    PDF first, since this deployment has no Tika sidecar.
    """
    stages.upload(limit=limit, dry_run=dry_run, repair_pdf=repair_pdf)


@main.command()
def finalize() -> None:
    """Confirm consumption and write custom field values.

    Paperless' post_document can attach a custom field but never set its value,
    so Tax Year is PATCHed here once the consumer has produced a real document.
    Re-run until it reports "still consuming 0"; it only touches unconfirmed
    rows, so running it repeatedly is free.
    """
    stages.finalize()


@main.command()
@click.option("--dest", default=str(ARCHIVE_DIR), show_default=True,
              help="Where filed originals are moved to.")
@click.option("--source", default=str(Path.home() / "Downloads"),
              show_default=True,
              help="Tree the files came from; structure below it is preserved.")
@click.option("--include-duplicates", is_flag=True,
              help="Also sweep dupe_local/dupe_paperless into duplicates/.")
@dry_run_option
def archive(dest: str, source: str, include_duplicates: bool,
            dry_run: bool) -> None:
    """Move filed originals out of the scanned tree.

    Refuses to move anything it cannot prove is filed: a row must be uploaded
    AND its document id must appear in a live listing fetched right now.
    Nothing is overwritten and nothing is deleted.
    """
    stages.archive(dest=dest, source=source,
                   include_duplicates=include_duplicates, dry_run=dry_run)


@main.command("rename")
@click.option("--directory", default=None,
              help="Only rename files under this path.")
@click.option("--all", "all_files", is_flag=True,
              help="Rename every classified file, not just the generic names.")
@dry_run_option
def rename_cmd(directory: str | None, all_files: bool, dry_run: bool) -> None:
    """Rename scanner-default filenames after their actual content.

    scan0017.pdf becomes utility-disclosure-energy-costs-123-main-st-20260611.pdf.
    Only uninformative names (scan*, IMG_*, Untitled, Document*) are touched
    unless --all is given, and the state db follows the rename.
    """
    stages.rename(directory=directory, all_files=all_files, dry_run=dry_run)


@main.command("triage-errors")
def triage_errors() -> None:
    """Sort consume failures by cause and requeue the recoverable ones.

    Already-filed duplicates become dupe_paperless; transient faults and
    Ghostscript PDF/A failures go back to 'classified' for another upload
    (the latter want `upload --repair-pdf`). Anything unrecognised is printed.
    """
    stages.triage_errors()


@main.command()
@verbose_option
def renorm(verbose: bool) -> None:
    """Re-apply normalize.py to stored results, without re-running the model.

    Use after editing an alias table or a guarded tag: the model output is
    already on disk, only the snapping rules changed.
    """
    stages.renorm(verbose=verbose)


@main.command()
def retag() -> None:
    """Re-apply the path-based bulk-series rules to classified rows."""
    stages.retag()


@main.command()
@click.argument("pattern")
@click.option("--text", type=int, default=0, metavar="CHARS",
              help="Also print this many characters of extracted text.")
def show(pattern: str, text: int) -> None:
    """Inspect the stored record for the first file matching PATTERN."""
    try:
        stages.show(pattern, text=text)
    except LookupError as e:
        raise click.ClickException(str(e)) from e


@main.command("set")
@click.option("--to", type=click.Choice(DISPOSITIONS), required=True,
              help="Disposition to force.")
@click.argument("patterns", nargs=-1, required=True)
def set_cmd(to: str, patterns: tuple[str, ...]) -> None:
    """Override the model's call for every file matching PATTERNS.

    The override is stored separately from the model's own decision, so
    re-classifying will not undo it.
    """
    stages.set_disposition(to, patterns)


@main.command("email-setup")
def email_setup() -> None:
    """Print the one-time Google Cloud steps for Gmail access."""
    click.echo(mailfetch.__doc__)
    click.echo(f"client secret expected at: {mailfetch.CLIENT_SECRET}")
    click.echo(f"cached token will be at  : {mailfetch.TOKEN_PATH}")
    click.echo(f"client secret present    : {mailfetch.CLIENT_SECRET.exists()}")
    click.echo(f"token present            : {mailfetch.TOKEN_PATH.exists()}")


@main.command("email-fetch")
@click.option("--since", default="2026-01-01", show_default=True,
              metavar="YYYY-MM-DD", help="Only messages on or after this date.")
@click.option("--out", "out_dir", default=str(Path.home() / "intake" / "mail"),
              show_default=True, help="Where attachments are written.")
@click.option("--query", default=None,
              help="Raw Gmail query, replacing the default "
                   "'has:attachment -in:sent after:<since>'.")
@click.option("--max-messages", type=int, default=1000, show_default=True)
@click.option("--deny", multiple=True, metavar="SUBSTRING",
              help="Extra sender substrings to skip (repeatable).")
@dry_run_option
def email_fetch(since: str, out_dir: str, query: str | None,
                max_messages: int, deny: tuple[str, ...],
                dry_run: bool) -> None:
    """Download document attachments from Gmail into a staging directory.

    Recurring software/subscription receipts are skipped by default (see
    DENY_SENDERS in mailfetch.py), as are attachments under 12 KB, which are
    signature logos rather than documents. Feed the result to the normal
    pipeline: scan that directory, then extract/classify/upload.
    """
    q = query or f"has:attachment -in:sent after:{since.replace('-', '/')}"
    click.echo(f"query: {q}")
    svc = mailfetch.service()
    tally = mailfetch.fetch(svc, Path(out_dir), q,
                            max_messages=max_messages, extra_deny=deny,
                            dry_run=dry_run)
    click.echo(f"email-fetch: {tally}")
    if not dry_run and tally["saved"]:
        click.echo(f"\nNext:  pintake scan {out_dir} && pintake extract "
                   f"&& pintake classify")


@main.command()
def config() -> None:
    """Show the endpoints and paths this install will use."""
    rows = [
        ("paperless", PAPERLESS_URL),
        ("llm", f"{LLM_URL}  ({LLM_MODEL})"),
        ("state db", str(DB_PATH)),
        ("archive dir", str(ARCHIVE_DIR)),
    ]
    for k, v in rows:
        click.echo(f"{k:12} {v}")


if __name__ == "__main__":
    main()
