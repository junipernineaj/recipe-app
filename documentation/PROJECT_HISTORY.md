# Project History

This is a running log of how this project has actually evolved — what
Tony asked for, and what got built in response, in the order it happened.
It's meant to be readable end to end by anyone curious about how a
personal cookbook-digitization side project turned into a two-repo
pipeline-plus-web-app system, not just a commit list.

**How this file is kept up to date:** every time a meaningful change is
made through a conversation with Claude, a new entry gets appended to the
end, in brief — what was asked, and what was done about it. If you're
starting a fresh Claude session on this project, point it at this file
and ask it to keep adding to it; a new session has no memory of this
convention unless it's told. New entries are appended, not merged into
the existing text — this file only grows.

**A note on sourcing:** the entries through 2026-09-25 below are
reconstructed from the repo's own git history (commit messages, several
of which already explain their own reasoning in detail) rather than a
direct transcript, since they predate the conversation this log was
first written from. From the "Cleaning up a bad extraction" entry
onward, entries reflect the actual conversation they came from.

---

## 2026-09-20 — Getting off the ground

Built the initial skeleton: a FastAPI + SQLite + HTMX web app for
browsing a personal recipe collection. First pass covered a recipe list,
a detail page, a form for adding recipes by hand, delete (via HTMX, with
the fiddly 204-vs-200 swap behaviour ironed out), and the cookbook-themed
CSS styling that's stuck around since.

## 2026-09-21 — A dashboard for the book library

Added `/books`, a dashboard showing every book's live pipeline status
(needs OCR, OCR'd, compressed, etc.) rather than just the recipes that
had made it through — with clickable status badges to filter the list,
sortable columns, and click-through to view whichever copy of a book
(compressed, OCR'd, or original) actually exists on disk. Along the way,
fixed a join bug that was hiding real compression progress because it
only matched on OCR output paths.

## 2026-09-22 — Bringing the pipeline into the same repo

The OCR/compression pipeline scripts (previously separate) were brought
into this repo alongside the web app, and tuned based on real books going
through it: added flags to `ocr_pass.py` for fixing mis-oriented scans
(`--force-ocr`, `--rotate-pages-threshold`, `--file-list`), relaxed the
OCR/recompression text-match threshold after seeing it reject
legitimately-fine pages, fixed the compressed-file-size display to show
the real compressed size rather than the original scan size, and
temporarily hid the "Not a cookbook" button to prevent accidental clicks
while it was still being worked out.

## 2026-09-23 — Writing it down

Added `documentation/ARCHITECTURE.md` — the "where does everything live
and how does it fit together" reference, covering the three-storage-folder
rule (holding pen → permanent OCR archive → compressed serving copies),
the database schema, and the gotchas that had already come up. Also fixed
`ocr_pass.py` to pick up `native_force_reocred` rows alongside
`needs_ocr`, which it had been silently skipping.

## 2026-09-24 — The ghost-row incident, and the extraction pipeline taking shape

The busiest single day so far. Clearing out the scan holding pen wholesale
(rather than file-by-file, once each one was confirmed safe) exposed a
real bug: the inventory scan never checked whether a previously-recorded
file still existed on disk, so stale rows accumulated and ~667 already-
compressed books briefly lost their real status. Fixed with a proper
`--prune`/`--prune-only`/`--archive-dir` mechanism that only ever deletes
a row when the file is confirmed gone *and* has no processing history *and*
a same-named copy exists in the archive — and documented the incident and
the rule ("only clear a file out once it shows Readable Native / OCR Done
/ Compressed") in `ARCHITECTURE.md` so it wouldn't get rediscovered the
hard way again. Also fixed the compressed-file join to catch
natively-readable books, which had been going through a different code
path than OCR'd ones and falling through every query.

This is also the day recipe *extraction* itself came together, piloted
against a real book (Nigella Lawson):

- Fixed a bug where the extraction call assumed the model's response
  started with a text block, which broke once Sonnet 5's adaptive
  thinking put a thinking block first — and disabled adaptive thinking
  for extraction entirely, since it's plain transcription work, not
  reasoning, and the thinking tokens were both eating into the token
  budget and causing truncated-JSON failures.
- Added retry logic that grows the token budget on a truncated response,
  instead of repeatedly asking for valid JSON at a budget too small to
  hold it.
- Added `extract_recipes_local.py`: the same prompt, schema, and resumable
  design, pointed at a local Ollama model instead, to compare cost and
  quality before committing to paying per book via the Claude API. Added
  an `engine` column so both engines can run against the same book
  without corrupting each other's resume tracking.
- Instructed the extraction prompt to skip alphabetical indexes and
  glossaries (not just tables of contents), and added a deterministic
  backstop that discards mostly-blank stub chunks outright rather than
  inserting them as recipes.
- Fixed the local Ollama model (qwen3:14b) occasionally writing the
  literal string `"false"` into `flagged_for_review`, which was being
  treated as a genuine (if odd) flag instead of "no flag".
- Capped Ollama generation length after one chunk ran away to 23,000+
  tokens and overflowed its context window, eating up to 30 minutes
  before the timeout finally caught it.

And the web app grew the review workflow needed to actually curate what
extraction produced: a `/review` queue listing every pending recipe
across all books and engines, an Approve button and pending-review banner
on the recipe detail page, an edit form so a flagged or slightly-wrong
extraction could be fixed in place rather than only approved or rejected
outright, admin/user access control gated on the Cloudflare Access email
header (Edit, Approve, Reject, and manual recipe entry all became
admin-only), and recipe reviews (a "made it" tick, 1–5 star rating, and
notes, attributed by Cloudflare Access identity). Recipe pages also
gained a link straight to the relevant page range in the original source
PDF, so any paraphrasing by the extraction engine is a non-issue — you
can always jump to the real wording.

## 2026-09-25 — Filtering and fixing what had gone live

Added a "Recipes done" checkbox per book (deliberately manual, not
auto-detected, since engine and chunk size vary too much run to run to
infer completion reliably) plus summary badges on `/books`. Added a
book-filter dropdown to the recipe search on the home page. Also caught
two things that had been written but never actually wired up: the
title-case template filter and the Notes section on the recipe detail
page — both fixed to actually render.

## Cleaning up a bad extraction

Jamie Oliver's *Jamie's 30-Minute Meals* had been extracted in full — 148
recipes — but the book's layout turned out not to suit the extraction
script (side-by-side recipe steps and time-coded columns don't
transcribe cleanly into a linear ingredients/method format). Tony asked
for all 148 to be deleted rather than manually reviewed one by one, and
decided to leave the book itself unmarked as "not a cookbook" for now
rather than exclude it from the library outright — noting he'd "be more
cautious when checking books" going forward. Also answered a quick
question about which CLI flag controls the pipeline's page-chunk size.

## Designing a QC system for extracted recipes

Tony raised that manual human review of every extracted recipe against
its source book — while mostly accurate — probably wasn't making full use
of data already on hand: `source_path`/`source_page` are stored per
recipe, so a recipe's original chunk text could be regenerated on demand
and cross-checked. He proposed a multi-rule checking system (starter
ideas: more than 2 ingredients, no stray `%` signs as an OCR-mangled-
fraction tell, every ingredient referenced somewhere in the method), plus
a possible second pass double-checking each recipe against its source PDF
chunk.

Landed on a two-phase design: **Tier 1**, deterministic checks with no
model call, cheap enough to run on every recipe automatically; **Tier 2**,
a selective LLM semantic recheck against the regenerated source-page text
— deliberately deferred, since it costs real tokens per recipe and the
groundwork (Tier 1) was worth getting right first. Tony approved this
approach explicitly, noting rules could be added to Tier 1 at any time
before Tier 2 gets built.

## Building Tier 1

Built `pipeline/recipe_qc.py`: a `recipe_qc_results` table (one row per
recipe per check, kept separate from the existing `flagged_for_review`
column since that's the *extraction model's own* signal rather than an
after-the-fact check), and a small registry of checks — each just a
plain function returning pass/fail plus a detail string, so a new rule is
one function and one line, nothing else needs to change. Shipped with
four hard checks (more than 2 ingredients, at least one instruction, no
`%` artifacts, no suspiciously short parsing-artifact lines) and one
advisory check (every ingredient referenced somewhere in the method,
flagged as advisory rather than hard since things like "salt and pepper
to taste" are expected false positives). Wired into both extraction
scripts so new recipes get checked automatically, plus a standalone
backfill CLI for everything already in the database. On the web app side:
QC badges and failed-check notes on the review queue, and a new
`/qc-issues` admin page for recipes that already got approved before a
check existed (or before a new rule was added) and so wouldn't otherwise
surface anywhere.

## Rounding out Tier 1: the extraction model's own warning, and a per-recipe view

Two follow-up requests. First, a 6th check — `no_extraction_warning` —
that simply surfaces the existing `flagged_for_review` column as a normal
QC row, so a recipe the extraction model itself flagged shows up
alongside every other check instead of only in its own separate spot.
Second, a full QC table directly on each recipe's own detail page
(admin-only), listing every check — hard and advisory both — with a green
tick or red cross and its detail text, so the full picture for one recipe
is visible without going through the review queue or `/qc-issues` first.

(One follow-up worth noting for anyone re-reading old badges: the review
queue's "QC: n/n" counter reflects how many checks were actually recorded
for that recipe the last time it was checked — a recipe checked before a
new rule was added will show a lower total until `recipe_qc.py` is
re-run. Not a bug, just a reason to re-run the backfill after adding or
changing a rule.)

## This document

Tony asked for a running summary of everything asked for and built so
far — readable end to end by anyone wanting the whole story — maintained
going forward by appending a short entry after each future change. This
file is the result.

## Sorting the review queue

Tony asked for a way to sort the review queue by book name and by QC
score, rather than only ever scrolling through it in extraction order --
sorting by QC score makes it easy to prioritise the clean-looking,
quick-to-approve recipes first; sorting by book makes it easy to focus on
(or deliberately postpone) one particular book at a time.

Added two clickable "Sort by" toggles above the queue, matching the
existing sort-header pattern already used on the books list (click to
sort, click again to reverse, with a ▲/▼ arrow showing the active
direction). QC score is the fraction of hard checks passed for that
recipe; a recipe that hasn't been through `recipe_qc.py` yet always sorts
to the bottom under a QC sort, in either direction, since there's nothing
to rank it by. Tested and confirmed working.

## Linking related recipes: "needs" and "works well with"

Tony pointed out that some chefs build a base sauce (a tomato sauce, say)
and then use it inside another recipe -- a puttanesca is that same tomato
sauce plus olives. He wanted a way to record that: a "needs X recipe"
link for a recipe that's built from another, and a looser "works well
with" link for recipes that just go well served together, each pointed
at an existing recipe rather than free text.

Added two multi-select dropdowns to the recipe edit page, both scoped to
recipes sharing the same source book (a cross-book reference felt too
fragile -- the other book might not even be approved yet). A recipe's own
page now shows a "Related Recipes" section listing what it needs, what
it's used as a base for (the reverse of "needs" -- e.g. the tomato sauce
page lists the puttanesca that needs it), and what it works well with.
"Works well with" only needs entering from one side -- it shows on both
recipes' pages either way. Deleting a recipe cleans up any relations
pointing to or from it. Tested and confirmed working.

## Catching OCR artifacts with a corpus-frequency check

Tony noticed the word "Ye" turning up in ingredient lists -- not
something a dictionary would catch, since it's genuine (if archaic)
English, but almost always a garbled fraction glyph in practice. Rather
than reach for an English dictionary, which would happily pass a
real-but-wrong word like that, the new `unusual_words` check instead uses
the recipe library itself: any word appearing in a recipe's ingredients
or instructions that turns up in zero *other* recipes anywhere in the
whole library gets flagged as advisory, on the theory that a word this
library has never used anywhere else is a far more reliable tell that
something got garbled than whether it happens to be "real" English.
Tested and confirmed working.

## Reflecting every QC check, and a way to manually clear advisory flags

With `unusual_words` bringing the total to seven checks, Tony pointed out
that the QC badge shown around the app was still only counting the five
hard checks -- by original design, to avoid the score being diluted by
heuristic advisory false positives -- and asked for two changes: show all
seven checks in every QC score/badge, and give advisory checks (only
advisory -- hard checks stay non-dismissible) a "Reviewed, OK" checkbox
so a human glance that confirms a flagged word or detail is genuinely
fine can clear it, without a future backfill run just re-flagging the
same thing again. An acknowledgment resets automatically if the
underlying detail text changes or the check starts passing outright, so
it only ever suppresses a flag that's still describing the exact same
thing someone already looked at. Tested and confirmed working.

## Backlog and library-size counts

Tony wanted a running reminder, visible at a glance, of how much is still
outstanding: a recipe count at the top of the QC Issues page (a nudge
about the technical debt of persisting problematic OCR extracts instead
of clearing them), a recipe count at the top of the Review Queue (a
reminder of recipes not yet visible to readers), and a recipe count plus
unique-book count on the main recipes page itself, visible to readers and
admin alike. All three needed no backend changes -- the underlying data
(`recipes` and the distinct-book list) was already flowing into each
page's template. Tested and confirmed working.

## Browsing the library by author

Thinking about the reader's experience rather than the admin's, Tony
suggested a page listing each book together with its author, since every
book is already named "Author - Title" in `source_book` by convention.
Added a new "Browse by author" page, reachable from the home page nav,
that groups every book with at least one approved recipe under its
author's name (a `source_book` that doesn't follow the naming convention
falls back to an "Unknown" author rather than being dropped), with each
book linking through to its own filtered recipe list. Tested and
confirmed working.

## Fixing the "Reviewed, OK" checkbox

Tony reported the advisory-check acknowledgment checkbox wasn't sticking
-- checking it looked like it worked, but a reload always showed it
unchecked again. The recipe detail page had never loaded htmx.js, unlike
every other page using `hx-post` forms, so the checkbox's `hx-*`
attributes were silently inert: clicking it fell back to a plain,
unconfigured form submission that just reloaded the page with a stray
query string, never reaching the route that actually saves the flag.
Fixed by adding the same htmx `<script>` tag every other page already
has. Tested and confirmed working.

## Outer-loop unit tests

Off the back of that bug, Tony asked for a standing list of checks that
should always pass after any change, rendered somewhere they can be
checked both automatically and by hand. Added `site_checks.py`: nine
read-only checks against the live site, covering things like every page
using `hx-*` attributes actually loading htmx (the exact class of bug
above), admin pages rejecting anonymous visitors and loading for admins,
no orphaned foreign keys between recipes and their QC/relation/review
rows, the QC backfill being current for every approved recipe, and the
home and author pages' displayed numbers actually matching the database.
Runs from the command line (so a cron job can call it) or from a new
admin-only `/unit-tests` page with a "Run now" button, so the same checks
serve as both the automatic and the manual check Tony wanted.

## Catching a real bug on the very first run

The first live run of the new checks immediately found something real:
`delete_recipe()` cleaned up `recipe_relations` when removing a recipe,
but never touched `recipe_qc_results` or `recipe_reviews`, so every
recipe ever deleted had quietly left its QC history and reviews stranded
in the database. Fixed the delete route to clean up all three tables
together, and did a one-off cleanup of the 15 rows already orphaned in
production. A quick, concrete payoff for having built the checks in the
first place.

## Running the app-level checks automatically on every push

Tony's wife suggested GitHub Actions; the data-dependent checks in
site_checks.py (orphaned rows, QC backfill, live counts) still need the
real ~700-book library, which only exists on the home server, so those
keep running via cron and /unit-tests there. But the app/code-level
checks -- routes loading, admin gating, htmx wired up, static assets
intact -- don't need real data at all. Added requirements.txt (this
project never had one), ci_fixture_db.py (builds a small throwaway
recipes.db with one seeded, fully-QC'd recipe, so every check has
something real to exercise instead of failing on a missing table), and a
.github/workflows/site-checks.yml workflow that installs dependencies,
builds the fixture, and runs site_checks.py on every push and pull
request. Verified it actually catches something: deliberately removed a
template file to reproduce the exact "half-committed feature" bug from
earlier in this project, and the workflow failed as expected. Tested and
confirmed working.

## The first real GitHub Actions run immediately failed -- for a good reason

The very first push after adding the workflow failed: importing main.py
alone, before any route is even hit, adds a column to inventory.sqlite (a
second database, entirely separate from recipes.db, that only the pipeline
scripts and the /books route touch) -- and that database doesn't exist at
all on a fresh GitHub checkout. Fixed ci_fixture_db.py to also build a
minimal, empty inventory.sqlite (reusing the real table-creation functions
from cookbook_inventory.py, ocr_pass.py, and compress_pass.py rather than
re-declaring their schemas by hand) so importing main.py, and /books,
both work the same way in CI as they do for real. Verified against a
genuinely fresh clone and an isolated HOME directory this time, to avoid
the same false confidence that let this slip through the first time.
Tested and confirmed working.

## Manually-set book title/author, and a precise /authors page

Tony noticed the filename (often a messy `...toOCR`-suffixed original scan
name) was the only thing standing in for a book's actual title and author
on /books, and /authors had no better source than parsing "Author - Title"
out of whatever got typed as `--book-title` at extraction time -- fragile,
and only available once a book had actually been extracted. Added `title`
and `author` columns to `inventory` (same self-migrating, admin-set-not-
computed pattern as `excluded` and `recipes_extracted`), with an "Edit"
button next to each book on /books swapping the row into an inline form
via htmx, same style as the existing checkbox/exclude controls.

The harder part was /authors, which reads `recipes.db`, an entirely
separate database from where the new fields live. There's no direct link
between a `recipes` row and its `inventory` row either -- extraction reads
from whichever copy of a file existed at the time (original, OCR'd, or
compressed), so `recipes.source_path` could be any one of the three.
`get_books_with_authors()` now builds a lookup mapping every path form a
book might have gone by back to its `inventory` row, and resolves through
`ocr_results`/`compress_results` to find it regardless of which stage a
recipe's `source_path` was captured at. When resolved, the manually-set
title/author wins (falling back per-field, not all-or-nothing, so setting
just one of the two still helps); when not -- an older book, or one
without a title/author set yet -- /authors displays exactly as it did
before, parsed from `source_book`. Deliberately left extraction itself
untouched: `--book-title` is still typed by hand each run, this only
cleans up what's displayed afterward. Tested with fixtures covering both
the resolved and fallback paths, plus the OCR/compress path-chasing.
Tested and confirmed working.

---

*Still on the list, deliberately deferred: Tier 2 (the LLM-based semantic
double-check of a recipe against its original PDF chunk).*
