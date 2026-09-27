From cdcbb74c0c2440165d64b2ca282cc5cb5e5d7e91 Mon Sep 17 00:00:00 2001
From: Claude <noreply@anthropic.com>
Date: Sun, 27 Sep 2026 18:50:48 +0000
Subject: [PATCH] Document the two-database split and the new CI setup in
 ARCHITECTURE.md

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JrThFu6QaBeCeX9TnJ53ss
---
 documentation/ARCHITECTURE.md | 128 +++++++++++++++++++++++++++++++---
 1 file changed, 119 insertions(+), 9 deletions(-)

diff --git a/documentation/ARCHITECTURE.md b/documentation/ARCHITECTURE.md
index 6b61e95..bd02245 100644
--- a/documentation/ARCHITECTURE.md
+++ b/documentation/ARCHITECTURE.md
@@ -1,7 +1,9 @@
 # Cookbook Digitization & Recipe App — Architecture & Familiarisation
 
 This doc is the "where does everything live and how does it fit together"
-reference for this repo. It covers two things that share one database:
+reference for this repo. It covers two things — and, despite what you
+might assume, two separate databases between them (see "The two
+databases" below):
 
 1. **The pipeline** (`pipeline/*.py`) — turns a folder of scanned cookbook
    PDFs into OCR'd, compressed, searchable copies.
@@ -47,9 +49,31 @@ fixed and covered by `--prune` below, but it's why the rule above is worth
 actually following rather than just clearing the whole folder whenever
 convenient.
 
-## The database
+## The two databases
 
-One SQLite file, shared by the pipeline and the app:
+This project has **two separate SQLite databases** — worth being
+explicit about, since both get called "the database" in conversation, but
+they track completely different things and neither one substitutes for
+the other:
+
+- **`inventory.sqlite`** — the pipeline's book-tracking database (which
+  files exist, what state they're in). Lives under `~/cookbook-project/`.
+- **`recipes.db`** — the web app's own database (extracted recipe
+  content, reviews, QC results, relations between recipes). Lives in
+  `~/recipe-app/` itself, and is gitignored — nothing in it is ever
+  committed.
+
+`main.py` connects to *both* — `/books` and the library pages read
+`inventory.sqlite`; the home page, recipe pages, and everything under
+`/review` read `recipes.db`. Conflating them, or forgetting the app needs
+both, is an easy mistake to make: on 2026-09-27, the first real GitHub
+Actions run built a fixture `recipes.db` but not a fixture
+`inventory.sqlite`, and the whole app crashed on import — a module-level
+call adds a column to `inventory.sqlite` before any route is even hit,
+regardless of whether that particular route needs it. See "Continuous
+integration" below.
+
+### `inventory.sqlite`
 
 ```
 ~/cookbook-project/inventory.sqlite
@@ -86,6 +110,62 @@ updating `ocr_results.output_path` breaks the join between the two tables
 rather than erroring. If a book that should show as "compressed" doesn't,
 check for exactly this kind of stale pointer first.
 
+### `recipes.db`
+
+```
+~/recipe-app/recipes.db
+```
+
+The web app's own database — extracted recipe content and everything
+built on top of it. Referenced by a relative `sqlite3.connect("recipes.db")`
+throughout `main.py` and the pipeline's extraction scripts, so it's always
+wherever the process's working directory is (`~/recipe-app` for the app
+itself). Four tables:
+
+- **`recipes`** — one row per extracted (or manually-added) recipe.
+  Like `inventory.excluded`, its schema has grown by hand over time rather
+  than through a single `CREATE TABLE` anywhere in code — nothing here
+  creates this table; it's just assumed to already exist. Columns in use:
+  `title`, `source_book`, `source_path`, `source_page` /
+  `source_page_end`, `ingredients`, `instructions` (both newline-separated
+  text, not JSON), `servings`, `prep_time`, `cook_time`, `notes`, `status`
+  (`pending` at extraction -> `approved` via the review queue — there's
+  no `rejected` state; rejecting a recipe deletes the row outright, see
+  `delete_recipe` below), `engine` (which extraction pathway produced it,
+  e.g. `claude-api` vs. a local-model run), and `flagged_for_review` (the
+  extraction model's own "this looked off" note, also surfaced as one of
+  the QC checks below).
+- **`recipe_reviews`** — one row per `(recipe_id, user_email)`, created
+  automatically on app startup (`init_recipe_reviews_table()` in
+  `main.py`). `made_it` (0/1), `rating` (1-5), `review_text`. Re-submitting
+  the form updates the existing row rather than adding a new one, so
+  there's no history, just each person's current rating. See "Recipe
+  reviews" below.
+- **`recipe_relations`** — links between recipes, also created
+  automatically on startup. `recipe_id` -> `related_recipe_id`, typed by
+  `relation_type`: `needs` (this recipe requires another already in the
+  library, e.g. a puttanesca needing a base tomato sauce — directional)
+  or `pairs_with` (a looser "goes well together" link — treated as
+  mutual at display time by reading both directions, so it only needs
+  entering from one side).
+- **`recipe_qc_results`** — one row per `(recipe_id, check_name)`,
+  created by `pipeline/recipe_qc.py`'s `init_qc_table()` (also called from
+  `main.py` on startup, so a fresh `recipes.db` never breaks the review
+  queue before a QC run has happened). Seven checks: five `hard`
+  (`min_ingredients`, `min_instructions`, `no_percent_artifacts`,
+  `no_short_lines`, `no_extraction_warning`) and two `advisory`
+  (`ingredients_used_in_method`, `unusual_words` — see
+  `pipeline/recipe_qc.py` for what each one actually catches).
+  `acknowledged` (0/1) lets a human-confirmed false positive on an
+  advisory check stay cleared until the underlying detail text changes or
+  the check starts passing outright.
+
+`delete_recipe` cleans up matching rows in all three of the other tables
+whenever a recipe is deleted — this wasn't always true (see
+`documentation/PROJECT_HISTORY.md`, "Catching a real bug on the very
+first run"), so there's no equivalent of `inventory`'s ghost-row problem
+here, as long as deletion keeps going through that one code path.
+
 ## Two separate venvs — don't mix them up
 
 This project has **two different Python virtual environments** on
@@ -234,7 +314,7 @@ worked:
    problem.
 3. **Isolate before re-running the whole book.** For a crash on a specific
    page (say page 268 of a 600-page book), extract just that page range
-   with `qpdf --pages <file> <file> N-M -- test.pdf` and run `ocrmypdf`
+   with `qpdf --pages <file> <file> N-M — test.pdf` and run `ocrmypdf`
    directly against the small slice with `--verbose 1`, piped to a log
    file. This turns a 10-40 minute wait-and-guess into a few seconds, and
    gives you the full untruncated error (our scripts only store the last
@@ -266,8 +346,10 @@ leaving the feature unbuilt.
 
 ## The web app
 
-- `main.py` connects directly to `~/cookbook-project/inventory.sqlite` —
-  there is no separate app-side copy of book data.
+- `main.py` connects directly to `~/cookbook-project/inventory.sqlite` for
+  book/library data (see "The two databases" above) — there's no separate
+  app-side copy of that. Recipe content itself lives in the separate
+  `recipes.db`, in this repo's own directory.
 - `/books` and `/books/search` show the library; sortable by filename,
   status, size, last-updated.
 - `/books/view?path=...` serves the actual file, falling back in this
@@ -338,7 +420,7 @@ How it works:
 ## Recipe reviews ("made it" / ratings)
 
 As of 2026-09-24, anyone who can view an approved recipe can tick "I've
-made this", leave a 1-5 rating, and leave a free-text note -- visible to
+made this", leave a 1-5 rating, and leave a free-text note — visible to
 everyone else who can see that recipe (this is a 3-person family site, so
 attribution is the point, not a privacy concern).
 
@@ -347,7 +429,7 @@ How it works:
 - Reuses the same `Cf-Access-Authenticated-User-Email` header as admin
   access (see "Admin access control" above), but for a different purpose:
   `current_user_email(request)` just reads it to attribute a review to a
-  real person, and does **not** gate anything -- every visitor who reaches
+  real person, and does **not** gate anything — every visitor who reaches
   the site gets one, not just admins. There's no separate login for this.
 - One row per `(recipe_id, user_email)` in a new `recipe_reviews` table
   (`recipes.db`, created automatically on startup if missing --
@@ -361,11 +443,39 @@ How it works:
   reviews on) an unapproved recipe still requires admin, via the same
   `recipe["status"] != "approved"` check used elsewhere.
 - If `current_user_email(request)` comes back `None` (no header at all),
-  the review form doesn't render -- this would only happen if the app were
+  the review form doesn't render — this would only happen if the app were
   reached some way that bypasses Cloudflare Access, which per "Admin
   access control" above is only a real possibility from the home LAN
   directly, not from the public URL.
 
+## Continuous integration
+
+As of 2026-09-27, `.github/workflows/site-checks.yml` runs
+`site_checks.py`'s checks automatically on every push and pull request, in
+GitHub's own cloud runner — not on junipernine2, and with no access to
+the real ~700-book library or the real `inventory.sqlite`. That means it
+can only exercise the checks that don't depend on real data: routes
+loading, admin gating, htmx being wired up where needed, static assets
+being intact. The checks that matter most for catching *data* problems
+(orphaned rows, a stale QC backfill, live counts not matching) still only
+run for real on junipernine2, via cron and the `/unit-tests` admin page —
+this workflow doesn't replace that, it catches a different, earlier class
+of bug: something that breaks the app itself before a change is ever
+pulled to the server, like a page referencing a template that never got
+committed.
+
+`ci_fixture_db.py` builds the small, throwaway databases this needs — one
+seeded, fully-QC'd recipe in a fixture `recipes.db`, and a schema-only
+fixture `inventory.sqlite` (see "The two databases" above for why both are
+required just for the app to import cleanly, let alone serve pages). It
+refuses to run if either database already exists, so there's no path by
+which it could ever touch real data. Like the `recipes`/`inventory` tables
+themselves, both fixtures are hand-maintained against whatever columns the
+app currently selects — if a future feature queries a column that isn't
+set up in `ci_fixture_db.py`, CI will fail with a clear "no such column"
+error rather than silently passing; when that happens, add the column
+there too.
+
 ## Phase 2 (not started): recipe extraction
 
 Longer-term, individual recipes get extracted out of these digitized
-- 
2.43.0

