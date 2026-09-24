# Cookbook Digitization & Recipe App — Architecture & Familiarisation

This doc is the "where does everything live and how does it fit together"
reference for this repo. It covers two things that share one database:

1. **The pipeline** (`pipeline/*.py`) — turns a folder of scanned cookbook
   PDFs into OCR'd, compressed, searchable copies.
2. **The web app** (`main.py`, `templates/`) — a FastAPI + HTMX site that
   browses the resulting library and (eventually) individual recipes.

If you only remember one thing from this page, remember the three-folder
rule in the next section — it's the thing that's caused the most confusion
so far.

## The three storage locations (read this first)

All three live on the external drive, `/media/aj9/Juniper13/`. They are
**not** interchangeable, and each has exactly one job:

| Folder | Role | Kept forever? |
|---|---|---|
| `Books/Cookbooks/` | **Holding pen.** Where new scans land before processing. Once a file's OCR'd copy is safely in `cookbook_ocr_output`, the original here is no longer needed for anything the pipeline does. | No — it's an inbox, not an archive. |
| `cookbook_ocr_output/` | **The permanent archive.** Once OCR'd, a book's full-size, searchable-text copy lives here for good, regardless of size. This is the canonical "original" going forward. | **Yes, always.** |
| `cookbook_ocr_compressed/` | **The serving copies.** Smaller, re-compressed versions of the OCR'd files, generated from `cookbook_ocr_output`. This is what the app actually links to day-to-day. | Yes, but regenerable from `cookbook_ocr_output` if ever lost. |

Rule of thumb: **new files go into `Books/Cookbooks`, nothing gets deleted
from `cookbook_ocr_output`.** If disk space ever needs freeing up, the
candidate is `Books/Cookbooks` (once a file's OCR'd copy is confirmed safe),
never the OCR archive.

**Only clear a file out of `Books/Cookbooks` once it shows up as `Readable
Native`, `Ocr Done`, or `Compressed`** in the app or in `inventory.status` /
`ocr_results`. A file that's still `Needs Ocr` (never actually OCR'd yet)
has no record anywhere else — moving or deleting it before it's processed
loses it for good, ghost-row cleanup or not.

On 2026-09-24, clearing out `Books/Cookbooks` wholesale (rather than file by
file, once done) exposed a real bug: `cookbook_inventory.py`'s scan never
checked whether a previously-recorded file still existed on disk, so
`inventory` accumulated rows for files long gone from the holding pen. Those
"ghost rows" first showed as an inflated Needs OCR count on the dashboard
(433 instead of 2); a first attempt at cleaning them up via a one-off
`DELETE` script actually deleted the `inventory` row for ~667 already-
compressed books outright, which silently dropped them from *every*
dashboard count instead of showing their real status. Both issues are now
fixed and covered by `--prune` below, but it's why the rule above is worth
actually following rather than just clearing the whole folder whenever
convenient.

## The database

One SQLite file, shared by the pipeline and the app:

```
~/cookbook-project/inventory.sqlite
```

**Always use the full absolute path to this file.** SQLite silently
*creates* a new empty database if you point it at a path that doesn't
exist yet (a relative `--db inventory.sqlite` run from the wrong directory
has bitten us more than once) — it never errors, it just quietly starts
you off with a blank DB.

Three tables:

- **`inventory`** — one row per file found under the holding pen.
  `path` (original file location) is the primary key. Key columns:
  `status` (`readable_native` / `readable_via_calibre` / `needs_ocr` /
  `encrypted` / `unsupported` / `error` / `unsupported_extension`),
  `size_bytes`, `excluded` (1 = flagged "not a cookbook", hidden from the
  app and skipped by OCR/compress).
- **`ocr_results`** — one row per file OCR'd. `path` matches
  `inventory.path`. `output_path` points into `cookbook_ocr_output`.
  `ocr_status` is `ocr_success_verified` / `ocr_ran_low_text` / `failed` /
  `skipped_not_pdf`.
- **`compress_results`** — one row per file compressed. `path` here is the
  **OCR output path** (i.e. matches `ocr_results.output_path`, not the
  original `inventory.path`). `output_path` points into
  `cookbook_ocr_compressed`. `status` is `verified_ok` / `text_mismatch` /
  `compressed_unverified` / `failed`.

Because `compress_results.path` keys off the OCR output path rather than
the original, moving/renaming files in `cookbook_ocr_output` without
updating `ocr_results.output_path` breaks the join between the two tables
— the app will fall back to serving an older/uncompressed copy silently
rather than erroring. If a book that should show as "compressed" doesn't,
check for exactly this kind of stale pointer first.

## Two separate venvs — don't mix them up

This project has **two different Python virtual environments** on
junipernine2, and running a script with the wrong one produces confusing
`ModuleNotFoundError`s:

- **`~/recipe-app/venv`** — for the web app. Activate this before running
  `uvicorn main:app`.
- **`~/cookbook-project/venv`** — for the pipeline scripts. Activate this
  before running anything in `pipeline/`.

Also make sure you `cd` into the right directory first — `uvicorn` needs
to be run from `~/recipe-app` (so it can find `main.py`), and the pipeline
scripts should be run from `~/recipe-app/pipeline`.

## The pipeline, stage by stage

All three scripts are in `pipeline/` and are **safe to re-run** — pass
`--resume` and each one skips anything already recorded as done (a prior
*failure* still gets retried automatically; only a genuine success is
skipped).

### Stage 1 — `cookbook_inventory.py` (classify what's in the holding pen)

```
python3 cookbook_inventory.py --source /media/aj9/Juniper13/Books/Cookbooks \
    --db ~/cookbook-project/inventory.sqlite --csv ~/cookbook-project/inventory.csv --resume
```

Walks the holding pen, tries to extract text from every file (PDF, EPUB,
DOCX, RTF, TXT natively; mobi/azw/djvu/etc. via Calibre if installed), and
classifies each one. This is the step that decides whether a file needs
OCR at all.

Useful flags:
- `--resume` — skip anything already recorded in `inventory` (a prior
  `error` status still gets retried; only a real success is skipped).
- `--prune` — before scanning, remove `inventory` rows for files that have
  disappeared from `--source` *and* were never actually processed (no OCR
  history, no resolved status like `readable_native`). Anything with real
  processing history is protected regardless of where its file currently
  lives, so this never touches a done book. Requires `--archive-dir`
  (repeatable) to confirm a same-named copy actually exists somewhere
  before deleting anything; without it, missing files are only reported,
  never deleted.
- `--prune-only` — run just the prune step and print the summary, skip the
  scan entirely. **Run this (not `--prune`) first after a big clear-out**
  and check the Protected/Removed/Warning counts before trusting a real
  `--prune` run.

Typical prune, after processed books have been moved out of the holding pen:


```
python3 cookbook_inventory.py --source /media/aj9/Juniper13/Books/Cookbooks \
    --db ~/cookbook-project/inventory.sqlite --prune-only \
    --archive-dir /media/aj9/Juniper13/cookbook_ocr_output \
    --archive-dir /media/aj9/Juniper13/cookbook_ocr_compressed
```

### Stage 2 — `ocr_pass.py` (bake in searchable text)

```
python3 ocr_pass.py --db ~/cookbook-project/inventory.sqlite \
    --source /media/aj9/Juniper13/Books/Cookbooks \
    --output-dir /media/aj9/Juniper13/cookbook_ocr_output --resume
```

Runs `ocrmypdf` over everything Stage 1 marked `needs_ocr`, then
independently re-extracts text from the result to verify OCR actually
worked (rather than trusting `ocrmypdf`'s exit code). Originals are never
touched — output always goes to `cookbook_ocr_output`.

Useful flags beyond the basics:
- `--file-list <path>` — process an explicit list of file paths (one per
  line) instead of querying the DB. Use this for a targeted fix without
  touching the rest of the library.
- `--force-ocr` — discard any existing text layer and redo OCR from
  scratch. Needed when re-processing a file whose first OCR pass was bad
  (e.g. wrong orientation) — the default `--skip-text` mode leaves pages
  that already have *any* text layer alone, which would preserve the bad
  result.
- `--rotate-pages-threshold <N>` — lowers the confidence bar for
  `--rotate-pages`'s automatic per-page orientation correction (default
  ocrmypdf threshold is 14; we've used `2` successfully for batches of
  inconsistently-oriented scans, e.g. mixed-orientation recipe cards).
- `--timeout <seconds>` — default 2400 (40 min). Large/dense books
  (400+ pages, or a combined multi-volume PDF) can need much longer —
  we've used up to 7200s (2hr) for oversized single-file volumes.

**Known gap:** a small number of files fail with
`DecompressionBombError` (an embedded image with an absurd pixel count —
seen on a 576-page book with one wildly over-scanned image). `ocrmypdf`
has a `--max-image-mpixels` flag for exactly this, but it isn't wired
into `ocr_pass.py`'s CLI yet — that's a pending code change, not yet done.

### Stage 3 — `compress_pass.py` (shrink while preserving searchable text)

```
python3 compress_pass.py --db ~/cookbook-project/inventory.sqlite \
    --source /media/aj9/Juniper13/cookbook_ocr_output \
    --output-dir /media/aj9/Juniper13/cookbook_ocr_compressed --resume
```

Re-compresses images via Ghostscript (text objects pass through
untouched, so the OCR layer survives), then verifies success by
extracting text from before/after and comparing similarity
(`TEXT_MATCH_THRESHOLD`, currently `0.994` — relaxed down from an
originally-too-strict `0.999` after observing that genuine OCR/recompression
noise routinely lands in the 0.997–0.999 range; anything meaningfully
below that, e.g. ~0.98, is worth an actual manual look rather than assumed
noise).

Useful flags:
- `--file` / `--file-list` — target specific files directly, bypassing
  the normal `--source` directory scan. Essential for natively-readable
  PDFs that never went through OCR (and so never land under
  `cookbook_ocr_output`'s scan), or for re-verifying one specific fix.
- `--color-dpi` / `--gray-dpi` / `--mono-dpi` — compression targets
  (defaults 200/200/300).

### `weekly_refresh.sh` — chaining all three

`pipeline/weekly_refresh.sh` runs all three stages back to back with
`--resume`, logs to `weekly_refresh.log`, and prints a pass/fail summary.
**It currently has `CHANGE_ME` placeholders for `SOURCE_DIR`,
`OCR_OUTPUT_DIR`, and `COMPRESSED_OUTPUT_DIR`** and needs those filled in
with the real paths above before it's actually usable — it also predates
the pipeline scripts moving into this repo, so double check its `cd`
target and venv path match `~/recipe-app/pipeline` and
`~/cookbook-project/venv` before relying on it.

## Diagnosing a stuck/broken file

When something in this pipeline fails, this is roughly the order that's
worked:

1. **Get the real error**, not just the status. `ocr_results` and
   `compress_results` both store an `error_message` column — query it
   directly rather than guessing from the summary counts.
2. **Check the original with `qpdf --check`** before assuming the OCR
   output is broken — several "corrupt file" scares turned out to be a
   perfectly healthy original with the *OCR output* corrupted (usually
   from a timeout cutting `ocrmypdf` off mid-write). `qpdf --check` on the
   OCR'd copy vs. the original tells you which side actually has the
   problem.
3. **Isolate before re-running the whole book.** For a crash on a specific
   page (say page 268 of a 600-page book), extract just that page range
   with `qpdf --pages <file> <file> N-M -- test.pdf` and run `ocrmypdf`
   directly against the small slice with `--verbose 1`, piped to a log
   file. This turns a 10-40 minute wait-and-guess into a few seconds, and
   gives you the full untruncated error (our scripts only store the last
   1500 characters of `ocrmypdf`'s output in the DB, which can cut off the
   actual traceback).
4. A crash that reproduces identically on an isolated slice but *not* on
   a supposedly-different "clean" replacement copy is a sign the
   replacement isn't actually different content — worth an `md5sum`
   comparison before spending more time on it.

## The "not a cookbook" exclusion mechanism

`inventory.excluded` (0/1) lets a file stay physically on disk while being
hidden from the app and skipped by every pipeline stage. The backend is
fully wired up:

- `main.py`'s queries filter `AND excluded = 0` unless `show_hidden=true`
  is passed.
- `POST /books/exclude` (form param `path`) sets the flag.
- `ocr_pass.py`'s inventory query and `compress_pass.py`'s file-discovery
  both skip excluded paths.

**The "Not a cookbook" button was originally removed from the UI**
(`templates/books_list.html`) to avoid other viewers of the shared
`/books` page accidentally hiding files. As of 2026-09-24 it's back, but
gated behind `require_admin` (see "Admin access control" below) — so the
underlying concern is now handled by the admin check rather than by
leaving the feature unbuilt.

## The web app

- `main.py` connects directly to `~/cookbook-project/inventory.sqlite` —
  there is no separate app-side copy of book data.
- `/books` and `/books/search` show the library; sortable by filename,
  status, size, last-updated.
- `/books/view?path=...` serves the actual file, falling back in this
  order: **compressed copy (only if `verified_ok`) → OCR'd copy → raw
  original.** This fallback is why a file can silently serve a lower-
  quality version without erroring — if something's not showing the
  version you expect, this is the order to check.
- Deployed behind a Cloudflare Tunnel (`cloudflared`, systemd service) at
  `recipes.junipernine.com`, gated by Cloudflare Access. See
  `documentation/recipe-app-cloudflare-setup.pdf` for the original setup
  steps.
- Run with (see "Admin access control" below for `ADMIN_EMAILS`):
  `export ADMIN_EMAILS="you@example.com" && cd ~/recipe-app && source
  venv/bin/activate && uvicorn main:app --reload --host 0.0.0.0 --port
  8000`

## Admin access control

As of 2026-09-24, Edit, Approve, Reject, and "Not a cookbook" are
restricted to admins only — before this, anyone who could reach the site
at all (so, anyone Cloudflare Access let in: you, your wife, your
mother-in-law) could edit or delete any recipe or hide any book.

How it works:

- Cloudflare Access already authenticates every visitor (email allowlist
  + one-time-pin login) before a request reaches this app, and passes the
  verified email through in the `Cf-Access-Authenticated-User-Email`
  header.
- `main.py`'s `is_admin(request)` reads that header and checks it against
  `ADMIN_EMAILS`, an environment variable (comma-separated if a second
  admin is ever added) — not hardcoded, since this repo is public.
  `require_admin(request)` is the same check wrapped as a FastAPI
  `Depends()` that 403s non-admins outright.
- **`ADMIN_EMAILS` must be exported before starting the app** (see "Run
  with" above) or nobody is admin, including you — that's a deliberate
  fail-closed default, not a bug. If Edit/Approve/"Not a cookbook" go
  missing or start 403ing after a restart, check this first.
- **Known, accepted gap:** uvicorn binds to `0.0.0.0`, not `127.0.0.1`,
  so the app is reachable directly over the home LAN (e.g. from a Mac at
  `http://<junipernine2-LAN-IP>:8000`), not just through the Cloudflare
  Tunnel — this is deliberate, for convenience managing a headless box
  from elsewhere on the network. The consequence: the
  `Cf-Access-Authenticated-User-Email` header is only genuinely
  trustworthy when Cloudflare Access is the only thing that can set it,
  and that stops being true once the app is reachable directly. A device
  on the LAN that specifically crafted a request with that header set
  (e.g. `curl -H "Cf-Access-Authenticated-User-Email: you@example.com"`)
  would be treated as admin — nothing here verifies the header
  cryptographically. Ordinary browsing from a LAN device does *not*
  grant admin (browsers don't send this header on their own), so this
  isn't a hole a casual visitor stumbles into, but it isn't a hard
  boundary against anyone on the network who goes looking for it either.
  Accepted as-is (2026-09-24) given who's actually on this home network;
  if that ever changes, the fix is either binding back to `127.0.0.1`
  (loses direct-LAN access) or switching to verifying Cloudflare's JWT
  (`Cf-Access-Jwt-Assertion`) instead of trusting the plain header.
- Gated routes: `GET`/`POST /recipes/{id}/edit`, `POST
  /recipes/{id}/approve`, `DELETE /recipes/{id}`, `POST /books/exclude`,
  `GET`/`POST /recipes/new` (adding a recipe by hand), and the `/review`
  queue itself. Viewing a recipe that hasn't been approved yet
  (`GET /recipes/{id}`) also 403s for non-admins, even via a direct link.
- Templates receive `is_admin` in their context and hide the
  corresponding buttons/links client-side — that's convenience, not the
  actual security boundary, which is the server-side `require_admin`
  check.

## Phase 2 (not started): recipe extraction

Longer-term, individual recipes get extracted out of these digitized
books into the `recipes` table/UI that already exists for manual entry.
Not yet designed — flagged here so future-you remembers it's the next
big phase, not forgotten scope.

A separate, smaller idea logged for that phase: many filenames are messy
(e.g. `...toOCR` suffixes) and shouldn't be used as the display title for
extracted recipes. The plan agreed on was a `clean_title` column on
`inventory` (a display alias) rather than renaming files on disk, since
`inventory.path` is a primary key joined across tables.
