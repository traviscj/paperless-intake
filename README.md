# pintake — Downloads → Paperless-ngx intake

Scriptable, resumable, local-model document intake. Nothing leaves the LAN: text
extraction runs locally, classification runs against a local OpenAI-compatible
llama.cpp / llama-swap server.

    Paperless-ngx             (docker, paperless-webserver-1, sqlite)
    llama-swap :9292          (GPU box, qwen3-vl-30b-a3b)

See `examples/` for fictional documents run through each stage.

## Pipeline

    [email-fetch] -> scan -> sync -> extract -> classify -> neardupe
         -> consolidate -> rename -> report/taxonomy
         -> upload -> finalize -> archive

Every stage is idempotent and resumable; all state lives in `state.db`
(sqlite, one row per file). Re-running a stage only touches rows in the
matching status, so you can stop and restart at any point.

The short way is two commands with a look in between:

    pintake go            # scan, sync, extract, classify, neardupe, consolidate; prints a summary and stops
    pintake report        # what it decided; `pintake set --to skip|file PATTERN` overrules a call
    pintake go --file     # upload what is marked 'file', wait for Paperless to consume it, archive the originals

`go` first checks that Paperless answers and that the model can actually load. `classify` does the same, and if
the model server starts failing mid-run it stops after five failures in a row and leaves those files queued
(`RETRY` in the progress lines) instead of marking every one of them `ERR`. `ERR` now means the document itself
could not be classified; the reason is in `pintake show <name>`.

| stage      | what it does                                                        |
|------------|---------------------------------------------------------------------|
| `scan`     | walk a directory, MD5-hash eligible files, record them              |
| `sync`     | pull Paperless' checksums, flag exact dupes (both directions)       |
| `extract`  | `pdftotext -layout`; OCR fallback via `pdftoppm` + `tesseract`      |
| `classify` | local model → structured JSON, then snapped onto the live taxonomy  |
| `email-fetch` | pull document attachments out of Gmail into a staging dir        |
| `neardupe` | flag the same document saved under a second filename                |
| `rename`   | rename scanner-default filenames after their actual content         |
| `consolidate` | fold twin spellings of newly-proposed names into one each         |
| `renorm`   | re-apply `normalize.py` to stored results (no re-inference)          |
| `retag`    | re-apply path-based series rules to stored results                   |
| `report`   | dense triage table by disposition                                   |
| `taxonomy` | existing vs. proposed-new tags/correspondents/types (drift check)   |
| `upload`   | `POST /api/documents/post_document/` with full metadata             |
| `finalize` | resolve consume tasks to doc ids, set custom field values           |
| `archive`  | move filed originals out of the scanned tree, rewriting `state.db`  |

## Layout on disk

    ~/intake/pintake/     this project (state.db, cache, .venv, household.toml, .env)
    ~/intake/mail/        staging for Gmail attachments
    ~/.local/bin/pintake  shim -> uv run --project ~/intake/pintake pintake

`state.db`, `cache/`, `household.toml` and `.env` are local data and gitignored.

The shim uses `--project`, not `--directory`, so uv resolves the project and
its venv while leaving the working directory alone — `pintake scan .` still
means the directory you are standing in. Override the project location with
`PINTAKE_PROJECT`.

Note that a uv venv hard-codes absolute paths, so if you move this directory,
delete `.venv` and re-run `uv sync`.

## Install

Managed with [uv](https://docs.astral.sh/uv/). The interpreter is pinned in
`.python-version` and downloaded by uv, so this does not care what Homebrew
does to its `python@3.x` formulae:

    uv sync          # creates .venv, installs deps
    pintake --help   # via the ~/.local/bin shim, from anywhere


## Usage

`pintake --help` lists every stage; `pintake <stage> --help` documents its
options and defaults. `pintake config` prints the endpoints in use.

    pintake email-setup       # one-time Gmail OAuth instructions
    pintake email-fetch --since 2026-01-01 --dry-run

    pintake scan ~/Downloads
    pintake sync
    pintake extract --workers 6
    pintake classify --workers 3
    pintake neardupe -v
    pintake consolidate --dry-run
    pintake consolidate
    pintake rename --directory /Volumes/public/scans --dry-run
    pintake taxonomy          # what new tags would be created
    pintake report --disposition file
    pintake upload --dry-run
    pintake upload
    pintake finalize          # re-run until "still consuming 0"
    pintake archive --dry-run
    pintake archive

    pintake show <name-fragment> --text 2000   # inspect one file
    pintake set --to skip <pattern> [...]      # override a call

## Sources

Three, all through the same pipeline:

- **`~/Downloads`** — the original backlog.
- **`/Volumes/public/scans`** (the NAS scan drop) — scanner output lands as
  `scan0017.pdf`, so run `rename` after `classify` to name each file from its
  own content. `rename` only touches names that carry no information
  (`scan*`, `IMG_*`, `Untitled`, `Document*`) unless `--all` is passed, and the
  state db follows the rename. Naming matches what is already in that folder:
  `utility-disclosure-energy-costs-123-main-st-apt-2-20260611.pdf`.
- **Gmail** — `email-fetch` downloads document attachments to `~/intake/mail`. Needs a Gmail API OAuth client (`email-setup` prints the steps);
  the MCP is no use here because it hands out attachment ids but not bytes.

## Vision fallback

A scan with no text layer and failed OCR used to leave the text model guessing
from the filename — that is how a **social security card** came back classified
as a "social media profile", and a driver's licence was dropped as junk. When
extracted text is under ~120 characters, `classify` now renders the first page
and sends it to the vision model instead. Disable with `--no-vision`.

## Three traps this pipeline exists to avoid

These were all found the hard way; the guards are load-bearing, do not relax them.

**1. Never fuzzy-match documents for deduplication.** A batch of RSU
sell-to-cover confirmations shares a vest date, a correspondent and a title, and
is ~99% identical boilerplate — the only difference is a confirmation number.
Any similarity cutoff loose enough to catch a genuine duplicate also collapses
distinct transactions and silently drops real financial records. `neardupe`
therefore uses EXACT normalised-text equality and nothing else.

**2. A mailing address is not a property association.** The residence address is
printed on every brokerage confirmation, pay stub and bank statement the
household receives. Left alone, the model tagged all of them with the
residence's property tag, which poisons the per-property filtering a landlord actually files by. The
prompt now states this explicitly and it tests clean.

**3. The model will spray every tag it is shown.** Handing it the existing tag
list makes it reuse names (good) and also attach `taxes`, `financial`,
`contracts`, `leases` to anything (bad — a resume came back tagged
`financial, contracts, taxes`). Guarded tags in `normalize.py` drop any tag the
document text does not support.

## Dedupe

Paperless stores an MD5 of the original file but does not expose it over REST,
so `sync` reads it out of the container's ORM over SSH. Two dedupe passes:
exact matches against what Paperless already holds, and duplicate copies within
the scan set (`foo (1).pdf`). Paperless' own consumer rejects duplicate
checksums as a backstop, so a missed pass cannot create a double.

## Staying in sync with the existing taxonomy

This is the part that needed the most work — the model reads documents well but
invents its own vocabulary. Two mechanisms keep the archive's conventions
authoritative:

1. **Prompt injection.** Every request carries the live list of tags,
   correspondents and document types pulled from the API, plus household
   context (who the people are, which properties are actually owned) from
   `household.toml`.

2. **`normalize.py`**, applied to every result before it is stored:
   - *Correspondent aliasing* — an explicit alias table (`J.P. Morgan` →
     `JPMorgan Chase Bank`) plus a fuzzy match that ignores punctuation and
     corporate suffixes, so `Initech, Inc.` snaps to the existing `Initech Inc`
     rather than creating a twin.
   - *Namespace canonicalisation* — the model emits `tax_year:2023` and
     `taxYear:2022`; the archive uses `TaxYear:2023`. Prefixes are rewritten to
     the house spelling, values upper-cased.
   - *Guarded tags* — the model sprays `taxes`, `financial`, `contracts`,
     `leases` onto anything. A guarded tag is dropped unless the document text
     actually supports it (a resume ends up with just the person tag).
   - *Guarded person tags* — a person tag only survives if the document names
     that person (per-person regex in `household.toml`).
   - *Derived tags* — property and `TaxYear:` tags come from the structured
     `properties` / `tax_year` fields, never from the free-form tag list.

`upload` still does a final case-insensitive get-or-create, so nothing
duplicates even if normalisation misses. Run `taxonomy` before `upload` to see
every new name that would be created; anything showing `<-- 1 doc only` is
usually drift worth folding into an existing name.

## Two Paperless API quirks

**Custom field values cannot be set at upload.** `PostDocumentSerializer`
declares `custom_fields` as a `PrimaryKeyRelatedField`, so `post_document` can
only *attach* a field, never give it a value — passing `[{"field": 1, "value":
2022}]` returns `400 Incorrect type. Expected pk value, received str.` The
`finalize` stage therefore waits for the consume task to report `SUCCESS`, reads
`related_document` off it, and PATCHes `/api/documents/{id}/` with the real
value. It is safe to re-run; it only touches rows with no `paperless_id` yet.

**Word documents are rejected outright.** There are no Tika/Gotenberg sidecars
in this deployment, so `.doc`/`.docx` return `File type ... not supported`.
`convert.py` renders them to PDF locally instead (macOS `textutil` → HTML →
headless Chrome → PDF), cached by content hash under `cache/`. Adding two
containers for a handful of files was not worth it; revisit if Office documents
become common.

## Archiving filed originals

`archive` moves originals to `~/Downloads/archived-paperless` (override with
`--dest` or `PINTAKE_ARCHIVE`), preserving the directory structure below
`--source`, and rewrites each row's `path` so every other stage keeps working.
The archive directory name is in `SKIP_DIRS`, so a later `scan` walks past it
and re-scanning is a no-op.

It refuses to move anything it cannot prove is filed: a row must be `uploaded`
*and* its `paperless_id` must appear in a live listing of document ids fetched
at run time — not merely uploaded, not merely consumed. Nothing is ever
overwritten (`foo.pdf` → `foo (2).pdf`) and nothing is deleted.

`archive` is idempotent: a row whose path already sits under `--dest` is
counted as "already archived" and left alone. Without that check a second run
would bury everything one level deeper each time, because the path relative to
`--source` then starts with the archive directory itself.

`--include-duplicates` also sweeps `dupe_local`/`dupe_paperless` rows into an
`archived-paperless/duplicates/` subfolder. Those files were never uploaded —
their content is already in Paperless under another copy — so they are kept
separate and are the safe set to delete first if reclaiming space.

## Consumer throughput

Uploading is fast; *consuming* is the bottleneck — roughly **12 documents per
minute**, about 70 minutes for an 850-document batch.

**Do not raise `PAPERLESS_TASK_WORKERS` on this deployment.** It runs on sqlite,
which has a single writer lock, and extra consumers just collide on it.
Measured consume-failure rate ("database is locked") by worker count:

    6 workers -> 57% failed        3 workers -> 23% failed
    1 worker  ->  0% failed

`PAPERLESS_DB_TIMEOUT=60` is set and helps, but does not close the gap. The
parallelism itself works (5 concurrent consumes, ~11 cores busy) — the
bottleneck is one sqlite writer, so the real fix is **PostgreSQL**, after which
6+ workers are fine. Failures are recoverable: `triage-errors` marks them
transient and `upload` replays them.

Only ever restart Paperless **while the consume queue is empty**: Celery acks
tasks on receipt by default, so restarting with work outstanding can drop
already-reserved tasks, and they would have to be re-uploaded. Check first:

    curl -sH "Authorization: Token $TOKEN" "$URL/api/tasks/?page_size=200" \
      | jq '[.[]|select(.status=="PENDING" or .status=="STARTED")]|length' 

`finalize` is the safety net either way — anything that never reaches SUCCESS
keeps its row in `uploaded` with no `paperless_id`, so a re-run picks it up.

## Layout

    pintake/cli.py        click command surface — flags, help, defaults only
    pintake/stages.py     what each stage actually does (plain functions)
    pintake/classify.py   the prompt, the JSON schema, the model call
    pintake/normalize.py  snapping model output onto the existing taxonomy
    pintake/paperless.py  Paperless REST client
    pintake/convert.py    Word -> PDF, and PDF/A repair re-rendering
    pintake/extract.py    text extraction + OCR fallback
    pintake/db.py         sqlite schema and helpers

`stages.py` takes ordinary arguments rather than a parsed-argument object, so
any stage can be driven from a REPL or a test without going through the CLI.

## Adding a rule

New correspondent alias, person or property → edit `household.toml`; new
guarded tag or doctype alias → edit `pintake/normalize.py`;
then `uv run pintake renorm` to re-apply it without re-running the model.
Changed filing policy (what counts as `file` vs `skip`) → edit `RULES` in
`pintake/classify.py`, then re-classify the affected rows:

    sqlite3 state.db "UPDATE files SET status='extracted' WHERE <predicate>"

## Config

`~/.config/homelab/paperless.env` holds `PAPERLESS_URL` and `PAPERLESS_TOKEN`
(mode 0600). A project-local `.env` (gitignored; see `.env.example`) is read
first. Overridable via environment:
`PINTAKE_LLM_URL`, `PINTAKE_LLM_MODEL`, `PINTAKE_DB`, `PINTAKE_HOUSEHOLD`,
`PINTAKE_PAPERLESS_SSH`, `PINTAKE_PAPERLESS_CONTAINER`.

Household data — people and their tags, owned properties, the residence,
own-entity regex, correspondent aliases, bulk-series path rules — lives in
`household.toml` (gitignored). Start from `household.example.toml`:

    cp household.example.toml household.toml && $EDITOR household.toml

Model notes: `qwen3-vl-30b-a3b` (MoE, ~37 docs/min at 3 workers) is the default.
`qwen3.8-27b` is roughly as accurate but ~6/min — llama-server runs without
`-np`, so it has a single slot. `qwen3.6-35b-a3b` returns empty replies under a
JSON schema; do not use it. The 30b model lives in llama-swap's exclusive "ab"
group, so a run evicts whatever else shares that GPU — check nothing else is
running first.
