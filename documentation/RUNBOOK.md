# Cookbook Digitization & Recipe App — Operational Runbook

This is the "what do I actually type, and when" reference — every command
you might run by hand, its flags, and the situations that call for it.
For what things *are* and how they fit together (the data model, the
routes, the three-folder rule, the two databases), see `ARCHITECTURE.md`
— this doc deliberately doesn't repeat that background, it links back to
it instead.

## Environments (quick reference)

- **`~/recipe-app/venv`** — for the web app (`uvicorn main:app`).
- **`~/cookbook-project/venv`** — for the pipeline stages and
  `weekly_refresh.sh` in `pipeline/`.
- **No venv needed at all** for `recipe_qc.py`, `find_duplicate_books.py`,
  `remove_books_by_path_prefix.py`, or `remove_book_recipes.py` — all four
  are pure stdlib (`argparse`/`re`/`sqlite3`/`hashlib`), so plain `python3`
  works from anywhere as long as `--db` points at the real file.
- `cd` into the right directory first: `uvicorn` needs to run from
  `~/recipe-app`, the pipeline stage scripts from `~/recipe-app/pipeline`.

See `ARCHITECTURE.md` → "Two separate venvs" for why there are two in the
first place.

## Running the web app

```
export ADMIN_EMAILS="you@example.com"
cd ~/recipe-app
source venv/bin/activate
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

`ADMIN_EMAILS` must be exported *before* starting the app or nobody is
admin, including you — see `ARCHITECTURE.md` → "Admin access control" for
why that's a deliberate fail-closed default, not a bug.

## The pipeline, stage by stage

All three scripts are **safe to re-run** — pass `--resume` and each one
skips anything already recorded as done (a prior *failure* still gets
retried automatically; only a genuine success is skipped). See
`ARCHITECTURE.md` → "The pipeline, stage by stage" for what each one
actually does and why.

### Stage 1 — `cookbook_inventory.py`

```
python3 cookbook_inventory.py --source /media/aj9/Juniper13/Books/Cookbooks \
    --db ~/cookbook-project/inventory.sqlite --csv ~/cookbook-project/inventory.csv --resume
```

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

### Stage 2 — `ocr_pass.py`

```
python3 ocr_pass.py --db ~/cookbook-project/inventory.sqlite \
    --source /media/aj9/Juniper13/Books/Cookbooks \
    --output-dir /media/aj9/Juniper13/cookbook_ocr_output --resume
```

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

### Stage 3 — `compress_pass.py`

```
python3 compress_pass.py --db ~/cookbook-project/inventory.sqlite \
    --source /media/aj9/Juniper13/cookbook_ocr_output \
    --output-dir /media/aj9/Juniper13/cookbook_ocr_compressed --resume
```

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

## Diagnosing a stuck/broken pipeline file

When something in the pipeline above fails, this is roughly the order
that's worked:

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

## Running recipe QC checks

`pipeline/recipe_qc.py` populates `recipe_qc_results` (see
`ARCHITECTURE.md` → "The two databases" for the seven checks themselves).
`extract_recipes.py` and `extract_recipes_local.py` already call it
automatically on every newly-extracted recipe, so most of the time
nothing needs to be run by hand — this script is for backfilling recipes
that predate a check, or for re-running after a check's logic changes.
Needs no venv at all:

```
# Every recipe in the database
python3 pipeline/recipe_qc.py --db ~/recipe-app/recipes.db

# Just one book — --book matches source_book EXACTLY (case and
# punctuation included); if you're not sure of the exact string:
#   sqlite3 ~/recipe-app/recipes.db "SELECT DISTINCT source_book FROM recipes;"
python3 pipeline/recipe_qc.py --db ~/recipe-app/recipes.db --book "Nigella Lawson - Feast"

# Just one recipe, e.g. right after a manual edit
python3 pipeline/recipe_qc.py --db ~/recipe-app/recipes.db --recipe-id 42
```

## Finding and removing exact-duplicate books

Two small, standalone utilities in `pipeline/` for when the same book
ends up registered twice in `inventory` — not part of the regular
pipeline flow above, just there for whenever it's needed again. Needs no
venv at all.

- **`find_duplicate_books.py`** — resolves each book to the same file
  `/books/view` would actually serve (compressed, if `verified_ok` →
  OCR'd → the raw original) and hashes it, then reports every group of
  books whose resolved files are byte-identical. Read-only — it never
  changes anything, it just prints (or `--csv`-exports) the groups it
  finds, tier by tier (`compressed`/`ocr`/`original`), for you to decide
  what to do with. Deliberately exact-match only (zero false positives,
  at the cost of missing near-duplicates like a book re-scanned from a
  different physical copy).

  ```
  python3 pipeline/find_duplicate_books.py
  python3 pipeline/find_duplicate_books.py --csv duplicate_books_report.csv
  python3 pipeline/find_duplicate_books.py --include-excluded
  ```

- **`remove_books_by_path_prefix.py`** — removes every `inventory` row
  under a given folder prefix, along with its `ocr_results` and
  `compress_results` rows (walking the same original → OCR output →
  compressed output chain, so it doesn't leave orphaned rows behind).
  Written for the incident below, but generic — reusable for any similar
  accidental re-scan of a subfolder. Safe by default: with no flags it
  only *reports* what it would delete; nothing touches the database until
  `--apply`, and files on disk are only ever removed with
  `--apply --delete-files` together, and even then only the OCR'd/
  compressed copies, never the original. Doesn't touch `recipes.db` — see
  `ARCHITECTURE.md` → "The two databases" for why that's fine.

  ```
  python3 pipeline/remove_books_by_path_prefix.py "/media/aj9/Juniper13/Books/Cookbooks/Keeping/"
  python3 pipeline/remove_books_by_path_prefix.py "/media/aj9/Juniper13/Books/Cookbooks/Keeping/" --apply
  python3 pipeline/remove_books_by_path_prefix.py "/media/aj9/Juniper13/Books/Cookbooks/Keeping/" --apply --delete-files
  ```

  **The incident these were built for (2026-09-27):** a `Keeping`
  subfolder accidentally created directly under the `Cookbooks` source
  directory got picked up by `cookbook_inventory.py` as a second,
  separate set of books, even though every file in it was already
  registered under the folder above it — so each one got OCR'd and
  compressed all over again under a new `inventory.path`, showing up on
  `/books` as an exact duplicate of a book already in the library. The
  `Keeping` folder itself was gone by the time this was diagnosed;
  `find_duplicate_books.py` is what surfaced the pattern (a cluster of
  hash-identical books), and `remove_books_by_path_prefix.py` cleaned up
  the leftover rows once the folder's former path was known.

## Extracting recipes from a book (Phase 2)

Two interchangeable scripts, both writing into the same `recipes.db` with
the same schema and the same chunking logic -- see "Reprocessing a book
with different chunk settings" below for why `chunk_index`/`--resume`
behave the way they do, and the `engine` column on `recipes` for how
recipes from either one are told apart afterward. Needs the
`~/cookbook-project/venv` (same one the pipeline stages above use).

Both scripts take `--pdf` as just the book's name (the same "Author -
Title" string you pass to `--book-title`), not a full path. It resolves
inside `/media/aj9/Juniper13/cookbook_ocr_compressed` with `.pdf` appended
automatically, since that's where every book actually lives. Pass
`--fullpath` if you genuinely need to point at a PDF somewhere else, in
which case `--pdf` is used exactly as given.

### `extract_recipes.py` — via the Claude API

```
export ANTHROPIC_API_KEY=sk-ant-...
cd ~/recipe-app/pipeline
python3 extract_recipes.py --db ~/recipe-app/recipes.db \
    --book-title "Author - Title" --pdf "Author - Title" \
    --resume
```

Book names always have spaces in them ("Author - Title"), so **quote both
`--pdf` and `--book-title`** — an unquoted value gets split by the shell
into separate words, and argparse fails with a confusing "unrecognized
arguments" error naming whatever came after the first space, not a
helpful "not found." This still applies now that `--pdf` is just a name
rather than a full path -- shorter, but the spaces (and the need to quote
around them) haven't gone anywhere.

Flags:
- `--pdf` (required) — the book's name, resolved inside
  `/media/aj9/Juniper13/cookbook_ocr_compressed` with `.pdf` appended. A
  trailing `.pdf` you include yourself is tolerated (stripped, then
  re-added), so passing the filename doesn't double up.
- `--fullpath` — treat `--pdf` as a full path instead of a name inside
  that folder, for a PDF that lives somewhere else.
- `--book-title` (required) — display name stored as `source_book`,
  "Author - Title" convention.
- `--db` (required) — normally `~/recipe-app/recipes.db`.
- `--pages-per-chunk` (default **7**, locked in 2026-09-28 -- see
  "Reprocessing a book with different chunk settings" below for why a
  smaller value catches recipes a larger one silently drops, at the cost
  of more API calls).
- `--model` (default `claude-sonnet-5`; also accepts
  `claude-haiku-4-5-20251001`) — real, measured cost per book comes
  straight from the API's own token usage, printed at the end of the run,
  not an estimate.
- `--resume` — skip chunks already recorded in `extraction_log` for this
  book and engine. Only safe when `--pages-per-chunk` hasn't changed
  since the last run on this book — see below.
- `--sample N` — only process the first N chunks, for a quick smoke test
  before committing to the whole book.

Recipes land with `status='pending'` — nothing shows up on the live site
until reviewed and approved via the review queue.

### `extract_recipes_local.py` — via a local Ollama model

Same schema, same chunking logic, same extraction prompt — imports the
shared pieces straight from `extract_recipes.py`, so the two are a fair
side-by-side comparison rather than apples to oranges. Cost is $0.00; the
real constraint is GPU time, so this reports elapsed time per chunk
instead of a dollar figure. Requires Ollama running and reachable, with
the model already pulled.

**Defaults to `qwen3:14b`** — settled on after comparing it against the
script's original `qwen2.5:14b` fallback:

```
cd ~/recipe-app/pipeline
python3 extract_recipes_local.py --db ~/recipe-app/recipes.db \
    --book-title "Author - Title" --pdf "Author - Title" \
    --resume
```

Flags (beyond the ones shared with `extract_recipes.py` above):
- `--model` (default `qwen3:14b`) — an Ollama model tag; must already be
  pulled (`ollama pull qwen3:14b`). Pass `--model qwen2.5:14b` (or
  anything else you've pulled) to use a different one.
- `--ollama-host` (default `http://localhost:11434`, i.e. Ollama running
  on junipernine2 itself) — only needs changing if pointing at a
  different machine, e.g. the local-AI box.

Known limitation, same as the API version: chunks don't overlap, so a
recipe straddling a chunk boundary can come out incomplete or duplicated.
Worth knowing on top of that: a smaller local model is more likely to
miss -- or fail to flag -- a subtle OCR-garbling judgment call than Sonnet
was in testing. That's exactly what the `engine` column is for: review
can be filtered by which engine produced a given recipe.

## Reprocessing a book with different chunk settings

See "Extracting recipes from a book (Phase 2)" above for the full flag
reference for both `extract_recipes.py` and `extract_recipes_local.py` —
this section only covers what's different about running either one again
on a book that's already been extracted once.

If a book's recipe coverage looks thin, the first thing to check is what
`--pages-per-chunk` it was extracted with (default **7**, as of
2026-09-28 -- it used to default to 15). Smaller chunks tend to catch
more: `extract_recipes.py`'s `call_claude()` gives each chunk a token
budget (16000, doubling to a max of 32000 if the model's response gets
cut off mid-JSON) and retries a truncated or unparseable response up to
twice more -- but if all three attempts still fail, the whole chunk is
abandoned and returns no recipes at all, silently. A denser 15-page chunk
is more likely to blow that budget than a 7-page one, so the failure mode
isn't "a few recipes missed" -- it's "every recipe in that chunk, gone."
7 was settled on after exactly this cost the Nigel Slater book its missing
recipes at the old default.

**Don't just re-run with `--resume` at a smaller chunk size.** `--resume`
skips any `(source_path, chunk_index, engine)` already in
`extraction_log` -- and `chunk_index` is just `i // pages_per_chunk`, a
plain sequential counter with no page numbers in it. Chunk 0 exists in
the log whether it was a 15-page or an 8-page run, so `--resume` at a new
chunk size will wrongly treat early chunks as already done and skip
re-extracting them, even though they cover completely different page
ranges than before. `--resume` is only safe when the chunk size hasn't
changed.

There's also no dedup on insert: `insert_recipe()` unconditionally adds
every recipe a chunk returns, so re-extracting without clearing the old
rows first just piles new rows on top of the old ones rather than
replacing them.

Two ways to handle this, both using `pipeline/remove_book_recipes.py`
(dry run by default -- always look at the report before `--apply`):

**Compare side-by-side (recommended)** -- re-extract straight into the
live database and let the review queue be the reconciliation step:

```
cd ~/recipe-app/pipeline
python3 extract_recipes.py --db ~/recipe-app/recipes.db \
    --book-title "Author - Title" --pdf "Author - Title"
```

No explicit `--pages-per-chunk` needed if you're just moving a book up to
the current default (7) -- pass it explicitly (e.g. `--pages-per-chunk 5`)
if you want to go smaller still. No `--resume` -- this is a fresh pass
over the whole book. The newly
extracted recipes land with `status='pending'` alongside the book's
existing `approved` ones (nothing already live is touched or hidden), so
both sets are visible in the review queue at once. Go through them,
approve whichever version of each recipe is better (usually the new one,
if the old one was missing entirely -- but check for the reverse too,
since a smaller chunk can occasionally split a recipe awkwardly across a
chunk boundary), and once you've picked winners, clear out the losing
`pending` duplicates:

```
python3 remove_book_recipes.py --book "Author - Title" --status pending
python3 remove_book_recipes.py --book "Author - Title" --status pending --apply
```

`--status pending` leaves the approved recipes (winners) and
`extraction_log` untouched -- there's nothing left to resume, so no
reason to clear the log.

**Clean replace** -- if you'd rather not review two overlapping sets at
all and just trust the smaller chunk size outright, clear everything for
the book first (recipes, reviews, QC results, relations, *and*
`extraction_log`, so `chunk_index` starts meaning something again), then
extract fresh:

```
python3 remove_book_recipes.py --book "Author - Title"
python3 remove_book_recipes.py --book "Author - Title" --apply --clear-extraction-log

cd ~/recipe-app/pipeline
python3 extract_recipes.py --db ~/recipe-app/recipes.db \
    --book-title "Author - Title" --pdf "Author - Title"
```

Same note as above on `--pages-per-chunk` -- the current default (7) is
used automatically unless you pass a different value. Everything still
lands as `pending` and needs the usual review-queue pass
before it's visible on the site -- this skips the side-by-side comparison,
not the review step.

## Running the site checks locally, before pushing

```
cd ~/recipe-app
source venv/bin/activate
export ADMIN_EMAILS="ci-admin@example.com"
python3 ci_fixture_db.py
python3 site_checks.py
```

**Only do this in a scratch checkout, not your real junipernine2 working
copy** — `ci_fixture_db.py` refuses to run if `recipes.db` or
`inventory.sqlite` already exist (both real databases do, on the actual
server), so this is really for a throwaway clone when you want to sanity
check a change before pushing it, the same way GitHub Actions will. See
`ARCHITECTURE.md` → "Continuous integration" for what these checks
actually cover (and, just as importantly, what they can't — anything
that depends on real data only gets exercised for real on junipernine2).
