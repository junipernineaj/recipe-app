# Cookbook Digitization & Recipe App — Architecture & Familiarisation

This doc is the "where does everything live and how does it fit together"
reference for this repo. It covers two things — and, despite what you
might assume, two separate databases between them (see "The two
databases" below):

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

## The two databases

This project has **two separate SQLite databases** — worth being
explicit about, since both get called "the database" in conversation, but
they track completely different things and neither one substitutes for
the other:

- **`inventory.sqlite`** — the pipeline's book-tracking database (which
  files exist, what state they're in). Lives under `~/cookbook-project/`.
- **`recipes.db`** — the web app's own database (extracted recipe
  content, reviews, QC results, relations between recipes). Lives in
  `~/recipe-app/` itself, and is gitignored — nothing in it is ever
  committed.

`main.py` connects to *both* — `/books` and the library pages read
`inventory.sqlite`; the home page, recipe pages, and everything under
`/review` read `recipes.db`. Conflating them, or forgetting the app needs
both, is an easy mistake to make: on 2026-09-27, the first real GitHub
Actions run built a fixture `recipes.db` but not a fixture
`inventory.sqlite`, and the whole app crashed on import — a module-level
call adds a column to `inventory.sqlite` before any route is even hit,
regardless of whether that particular route needs it. See "Continuous
integration" below.

### `inventory.sqlite`

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
  app and skipped by OCR/compress), `title` / `author` (NULL until set by
  hand via the "Edit" button on `/books` — never computed or extracted;
  see "Book titles and authors, set by hand" below).
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

### `recipes.db`

```
~/recipe-app/recipes.db
```

The web app's own database — extracted recipe content and everything
built on top of it. Referenced by a relative `sqlite3.connect("recipes.db")`
throughout `main.py` and the pipeline's extraction scripts, so it's always
wherever the process's working directory is (`~/recipe-app` for the app
itself). Four tables:

- **`recipes`** — one row per extracted (or manually-added) recipe.
  Like `inventory.excluded`, its schema has grown by hand over time rather
  than through a single `CREATE TABLE` anywhere in code — nothing here
  creates this table; it's just assumed to already exist. `source_book`
  is still the "Author - Title" string typed by hand at extraction time
  (see the `--book-title` flag) — `get_books_with_authors()` (used by
  `/authors`) now prefers the manually-set `inventory.title`/`.author`
  over parsing this string, per field, when `source_path` can be traced
  back to that book (see "Book titles and authors, set by hand" below);
  `source_book` itself is untouched either way. Columns in use:
  `title`, `source_book`, `source_path`, `source_page` /
  `source_page_end`, `ingredients`, `instructions` (both newline-separated
  text, not JSON), `servings`, `prep_time`, `cook_time`, `notes`, `status`
  (`pending` at extraction -> `approved` via the review queue — there's
  no `rejected` state; rejecting a recipe deletes the row outright, see
  `delete_recipe` below), `engine` (which extraction pathway produced it,
  e.g. `claude-api` vs. a local-model run), and `flagged_for_review` (the
  extraction model's own "this looked off" note, also surfaced as one of
  the QC checks below).
- **`recipe_reviews`** — one row per `(recipe_id, user_email)`, created
  automatically on app startup (`init_recipe_reviews_table()` in
  `main.py`). `made_it` (0/1), `rating` (1-5), `review_text`. Re-submitting
  the form updates the existing row rather than adding a new one, so
  there's no history, just each person's current rating. See "Recipe
  reviews" below.
- **`recipe_relations`** — links between recipes, also created
  automatically on startup. `recipe_id` -> `related_recipe_id`, typed by
  `relation_type`: `needs` (this recipe requires another already in the
  library, e.g. a puttanesca needing a base tomato sauce — directional)
  or `pairs_with` (a looser "goes well together" link — treated as
  mutual at display time by reading both directions, so it only needs
  entering from one side).
- **`recipe_qc_results`** — one row per `(recipe_id, check_name)`,
  created by `pipeline/recipe_qc.py`'s `init_qc_table()` (also called from
  `main.py` on startup, so a fresh `recipes.db` never breaks the review
  queue before a QC run has happened). Seven checks: five `hard`
  (`min_ingredients`, `min_instructions`, `no_percent_artifacts`,
  `no_short_lines`, `no_extraction_warning`) and two `advisory`
  (`ingredients_used_in_method`, `unusual_words` — see
  `pipeline/recipe_qc.py` for what each one actually catches).
  `acknowledged` (0/1) lets a human-confirmed false positive on an
  advisory check stay cleared until the underlying detail text changes or
  the check starts passing outright.

`delete_recipe` cleans up matching rows in all three of the other tables
whenever a recipe is deleted — this wasn't always true (see
`documentation/PROJECT_HISTORY.md`, "Catching a real bug on the very
first run"), so there's no equivalent of `inventory`'s ghost-row problem
here, as long as deletion keeps going through that one code path.

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
   with `qpdf --pages <file> <file> N-M — test.pdf` and run `ocrmypdf`
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

## Book titles and authors, set by hand

`inventory.title` and `inventory.author` (added 2026-09-27) let you set a
book's real title and author from `/books` directly, rather than relying
on its (often messy, `...toOCR`-suffixed) filename or on retyping
`--book-title` correctly at extraction time. Both are plain admin-set
fields, same spirit as `excluded` — NULL means "not set", nothing here
infers or extracts a value.

- `GET /books/edit-title-author?path=...` / `POST /books/set-title-author`
  / `GET /books/title-author-display?path=...` back the inline
  edit/save/cancel form on `/books` (`templates/book_title_author_edit.html`,
  `templates/book_title_author_display.html`), htmx-swapped in place like
  the existing checkbox controls. All three are gated behind
  `require_admin` (see "Admin access control" below).
- Setting only one of the two fields is fine — the other still falls back
  to its old source (filename for title, the `source_book` parse for
  author) wherever it's displayed.
- **`/authors` is the harder consumer**, because it reads `recipes.db`,
  a completely separate database from where `title`/`author` live (see
  "The two databases" above), and there's no direct foreign key between a
  `recipes` row and its `inventory` row — `recipes.source_path` could be
  the original file, the OCR'd copy, or the compressed copy, depending on
  which one happened to exist when extraction ran (`extract_recipes.py`
  accepts any of the three as `--pdf`). `get_books_with_authors()` calls
  `_build_inventory_title_author_lookup()` to build a one-time map from
  every path form a book might go by — original, OCR output, compressed
  output, chasing through `ocr_results`/`compress_results` the same way
  `/books/view`'s fallback chain does — back to that book's `(title,
  author)`. A resolved match wins per-field over the historical
  "Author - Title" parse of `source_book`; no match (an older book, or
  one that's had nothing set) leaves `/authors` displaying exactly as it
  always did.
- Deliberately doesn't feed back into extraction — `--book-title` is
  still typed by hand each run. This only cleans up how a book is
  *displayed* afterward, not how it's *extracted*.

## The web app

- `main.py` connects directly to `~/cookbook-project/inventory.sqlite` for
  book/library data (see "The two databases" above) — there's no separate
  app-side copy of that. Recipe content itself lives in the separate
  `recipes.db`, in this repo's own directory.
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
  `GET`/`POST /recipes/new` (adding a recipe by hand), the `/review`
  queue itself, and `GET /books/edit-title-author` / `GET
  /books/title-author-display` / `POST /books/set-title-author` (see
  "Book titles and authors, set by hand" above). Viewing a recipe that
  hasn't been approved yet (`GET /recipes/{id}`) also 403s for
  non-admins, even via a direct link.
- Templates receive `is_admin` in their context and hide the
  corresponding buttons/links client-side — that's convenience, not the
  actual security boundary, which is the server-side `require_admin`
  check.

## Recipe reviews ("made it" / ratings)

As of 2026-09-24, anyone who can view an approved recipe can tick "I've
made this", leave a 1-5 rating, and leave a free-text note — visible to
everyone else who can see that recipe (this is a 3-person family site, so
attribution is the point, not a privacy concern).

How it works:

- Reuses the same `Cf-Access-Authenticated-User-Email` header as admin
  access (see "Admin access control" above), but for a different purpose:
  `current_user_email(request)` just reads it to attribute a review to a
  real person, and does **not** gate anything — every visitor who reaches
  the site gets one, not just admins. There's no separate login for this.
- One row per `(recipe_id, user_email)` in a new `recipe_reviews` table
  (`recipes.db`, created automatically on startup if missing --
  `init_recipe_reviews_table()` in `main.py`). Re-submitting the form
  updates your existing row rather than adding a new one, so there's no
  history of past ratings, just your current one.
- `POST /recipes/{id}/review` writes it; `GET /recipes/{id}` reads all
  reviews for that recipe plus your own (pre-fills the form) and computes
  a simple "N of M made this" / average-rating summary.
- Same visibility rule as the recipe itself: reviewing (or viewing others'
  reviews on) an unapproved recipe still requires admin, via the same
  `recipe["status"] != "approved"` check used elsewhere.
- If `current_user_email(request)` comes back `None` (no header at all),
  the review form doesn't render — this would only happen if the app were
  reached some way that bypasses Cloudflare Access, which per "Admin
  access control" above is only a real possibility from the home LAN
  directly, not from the public URL.

## Continuous integration

As of 2026-09-27, `.github/workflows/site-checks.yml` runs
`site_checks.py`'s checks automatically on every push and pull request, in
GitHub's own cloud runner — not on junipernine2, and with no access to
the real ~700-book library or the real `inventory.sqlite`. That means it
can only exercise the checks that don't depend on real data: routes
loading, admin gating, htmx being wired up where needed, static assets
being intact. The checks that matter most for catching *data* problems
(orphaned rows, a stale QC backfill, live counts not matching) still only
run for real on junipernine2, via cron and the `/unit-tests` admin page —
this workflow doesn't replace that, it catches a different, earlier class
of bug: something that breaks the app itself before a change is ever
pulled to the server, like a page referencing a template that never got
committed.

`ci_fixture_db.py` builds the small, throwaway databases this needs — one
seeded, fully-QC'd recipe in a fixture `recipes.db`, and a schema-only
fixture `inventory.sqlite` (see "The two databases" above for why both are
required just for the app to import cleanly, let alone serve pages). It
refuses to run if either database already exists, so there's no path by
which it could ever touch real data. Like the `recipes`/`inventory` tables
themselves, both fixtures are hand-maintained against whatever columns the
app currently selects — if a future feature queries a column that isn't
set up in `ci_fixture_db.py`, CI will fail with a clear "no such column"
error rather than silently passing; when that happens, add the column
there too.

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
