"""Outer-loop regression checks for the web app itself -- the "did I just
break something obvious" tests Tony wants to always pass after a change.
Distinct from pipeline/recipe_qc.py, which checks the *data* (individual
recipes); this checks the *app* (routes, templates, page/database
consistency). Almost every check here is read-only against the live
recipes.db, so it's safe to run against the real production database at
any time: from the command line, from a cron job, or from the
/unit-tests admin page.

The exceptions are check_delete_recipe_cleans_up_related_rows,
check_clone_recipe_copies_correctly, and check_edit_recipe_saves_source_page,
which have to actually exercise their routes to mean anything (a purely
read-only check can only notice orphaned rows that already exist --
that's exactly why check_no_orphaned_foreign_keys didn't catch either of
the two times delete_recipe shipped broken; see PROJECT_HISTORY.md, "The
delete_recipe fix that wasn't"). Each creates its own throwaway fixture
recipe(s), tagged unmistakably as fixtures, and always deletes every trace
of them itself in a finally block -- regardless of whether the check
passes, fails, or raises -- so all three are still safe to run against
production: worst case, one briefly creates and then removes a 'pending'
recipe (or two) that was never visible on the site.

Usage:
    python3 site_checks.py            # run once, print every result, save it
    python3 site_checks.py --quiet    # same, but only print if something failed (for cron)
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from markupsafe import escape as _html_escape

import main
from pipeline.recipe_qc import CHECKS as _QC_CHECKS

REPO_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = REPO_ROOT / "templates"
RESULTS_PATH = REPO_ROOT / "site_check_results.json"

_INCLUDE_MARKER = '{% include "'
_HX_ATTR_NAMES = ["hx-get=", "hx-post=", "hx-put=", "hx-delete=", "hx-patch="]
_HTMX_SCRIPT_MARKER = "htmx.org"


def _find_includes(text):
    """Names in every {% include "name.html" %} tag in text -- plain string
    search rather than regex, so this file has no backslash escapes for a
    paste pipeline to mangle."""
    included = []
    pos = 0
    while True:
        start = text.find(_INCLUDE_MARKER, pos)
        if start == -1:
            break
        start += len(_INCLUDE_MARKER)
        end = text.find('"', start)
        if end == -1:
            break
        included.append(text[start:end])
        pos = end
    return included


def _admin_email():
    """One real address from ADMIN_EMAILS to use as the admin identity in
    these checks -- same env var main.py itself reads, so this only works
    when site_checks.py is run with that variable set the same way the app
    is. Checks that need it degrade to a skip rather than a false failure
    when it isn't set (see check_admin_pages_load_for_admin)."""
    if not main.ADMIN_EMAILS:
        return None
    return sorted(main.ADMIN_EMAILS)[0]


def _admin_headers():
    email = _admin_email()
    return {"Cf-Access-Authenticated-User-Email": email} if email else {}


def _template_text(name):
    return (TEMPLATES_DIR / name).read_text()


def _collect_included(name, seen=None):
    seen = seen if seen is not None else set()
    if name in seen or not (TEMPLATES_DIR / name).exists():
        return seen
    seen.add(name)
    for included in _find_includes(_template_text(name)):
        _collect_included(included, seen)
    return seen


def check_htmx_loaded_where_needed(client):
    """Every top-level page -- and everything it {% include %}s, directly
    or indirectly -- that uses an hx-* attribute must also load htmx.js
    itself, or those attributes are silently inert: a click just falls back
    to a plain, broken form submission instead of the htmx exchange the
    page expects. This is exactly the bug that made the QC "Reviewed, OK"
    checkbox look like it worked while never actually saving."""
    problems = []
    for path in sorted(TEMPLATES_DIR.glob("*.html")):
        text = path.read_text()
        if "<!DOCTYPE html>" not in text:
            continue  # a partial, only ever {% include %}d -- not its own page
        combined = "".join(_template_text(n) for n in _collect_included(path.name))
        uses_hx = any(name in combined for name in _HX_ATTR_NAMES)
        loads_htmx = _HTMX_SCRIPT_MARKER in text
        if uses_hx and not loads_htmx:
            problems.append(path.name)
    if problems:
        return False, "Page(s) use hx-* but never load htmx.js: " + ", ".join(problems)
    return True, None


def check_public_pages_load(client):
    for path in ["/", "/books", "/authors", "/search?q=test", "/feedback"]:
        r = client.get(path)
        if r.status_code != 200:
            return False, f"{path} returned {r.status_code}, expected 200"
    return True, None


def check_admin_pages_reject_anonymous(client):
    # Deliberately doesn't include /unit-tests: that route calls run_all()
    # itself, so checking it from inside a check would recurse.
    for path in ["/review", "/qc-issues", "/feedback/inbox"]:
        r = client.get(path, follow_redirects=False)
        if r.status_code not in (401, 403):
            return False, f"{path} returned {r.status_code} with no admin header, expected 401/403"
    return True, None


def check_admin_pages_load_for_admin(client):
    headers = _admin_headers()
    if not headers:
        return True, "skipped -- ADMIN_EMAILS isn't set in this environment"
    # Deliberately doesn't include /unit-tests -- see the comment above.
    for path in ["/review", "/qc-issues", "/feedback/inbox"]:
        r = client.get(path, headers=headers)
        if r.status_code != 200:
            return False, f"{path} returned {r.status_code} with a valid admin header, expected 200"
    return True, None


def check_static_css_intact(client):
    r = client.get("/static/style.css")
    if r.status_code != 200:
        return False, f"/static/style.css returned {r.status_code}"
    for needed in [".qc-badge", ".site-nav", ".author-groups"]:
        if needed not in r.text:
            return False, f"/static/style.css is missing an expected rule: {needed}"
    return True, None


def check_no_orphaned_foreign_keys(client):
    conn = sqlite3.connect("recipes.db")
    problems = []
    for label, sql in [
        ("recipe_relations.recipe_id", "SELECT COUNT(*) FROM recipe_relations WHERE recipe_id NOT IN (SELECT id FROM recipes)"),
        ("recipe_relations.related_recipe_id", "SELECT COUNT(*) FROM recipe_relations WHERE related_recipe_id NOT IN (SELECT id FROM recipes)"),
        ("recipe_reviews.recipe_id", "SELECT COUNT(*) FROM recipe_reviews WHERE recipe_id NOT IN (SELECT id FROM recipes)"),
        ("recipe_qc_results.recipe_id", "SELECT COUNT(*) FROM recipe_qc_results WHERE recipe_id NOT IN (SELECT id FROM recipes)"),
    ]:
        try:
            count = conn.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:
            continue  # table doesn't exist in this schema version -- not this check's job
        if count:
            problems.append(f"{count} orphaned row(s) in {label}")
    conn.close()
    if problems:
        return False, "; ".join(problems)
    return True, None


def check_delete_recipe_cleans_up_related_rows(client):
    """Creates two throwaway 'pending' recipes, links them with a review,
    a QC result, and a relation in both directions, deletes one of them
    through the real DELETE /recipes/{id} route, and confirms every
    related row actually disappeared with it.

    check_no_orphaned_foreign_keys above only ever notices orphans that
    already exist -- it can't tell a delete route that cleans up properly
    from one that doesn't, unless something has already been deleted and
    left a mess behind. That's exactly how delete_recipe shipped broken
    twice in a row (see PROJECT_HISTORY.md, "Catching a real bug on the
    very first run" and "The delete_recipe fix that wasn't") without
    either the CI fixture (one recipe, never deleted) or this same check
    ever flagging it. This one actually exercises the delete path instead
    of inspecting its aftermath, so a regression here fails immediately
    rather than waiting to be noticed as orphaned rows on production
    later.

    Uses status='pending' so these fixtures can never affect the
    approved-only counts the other checks rely on, and always cleans up
    after itself in a finally block -- including the second recipe, which
    the route under test never touches -- so a broken delete route still
    can't leave real rows behind, even when run against production."""
    headers = _admin_headers()
    if not headers:
        return True, "skipped -- ADMIN_EMAILS isn't set in this environment"

    marker = "__site_checks_delete_cleanup_fixture__"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"

    conn = sqlite3.connect("recipes.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO recipes (title, source_book, source_path, ingredients, instructions, status) "
        "VALUES (?, ?, 'fixture.pdf', 'ing', 'inst', 'pending')",
        ("Fixture A", marker),
    )
    recipe_a = cursor.lastrowid
    cursor.execute(
        "INSERT INTO recipes (title, source_book, source_path, ingredients, instructions, status) "
        "VALUES (?, ?, 'fixture.pdf', 'ing', 'inst', 'pending')",
        ("Fixture B", marker),
    )
    recipe_b = cursor.lastrowid
    cursor.execute(
        "INSERT INTO recipe_reviews (recipe_id, user_email, made_it, updated_at) VALUES (?, ?, 0, ?)",
        (recipe_a, "site-checks-fixture@example.com", now),
    )
    cursor.execute(
        "INSERT INTO recipe_qc_results (recipe_id, check_name, passed, severity, checked_at) VALUES (?, ?, 1, 'hard', ?)",
        (recipe_a, "__fixture_check__", now),
    )
    # Both directions: recipe_a authoring a relation, and recipe_b
    # authoring one back at recipe_a -- delete_recipe's cleanup query has
    # to match recipe_id OR related_recipe_id to catch both.
    cursor.execute(
        "INSERT INTO recipe_relations (recipe_id, related_recipe_id, relation_type, created_at) VALUES (?, ?, 'needs', ?)",
        (recipe_a, recipe_b, now),
    )
    cursor.execute(
        "INSERT INTO recipe_relations (recipe_id, related_recipe_id, relation_type, created_at) VALUES (?, ?, 'pairs_with', ?)",
        (recipe_b, recipe_a, now),
    )
    conn.commit()
    conn.close()

    try:
        r = client.delete(f"/recipes/{recipe_a}", headers=headers)
        if r.status_code != 200:
            return False, f"DELETE /recipes/{recipe_a} returned {r.status_code}, expected 200"

        conn = sqlite3.connect("recipes.db")
        leftovers = []
        if conn.execute("SELECT 1 FROM recipes WHERE id = ?", (recipe_a,)).fetchone():
            leftovers.append("recipes")
        if conn.execute("SELECT 1 FROM recipe_reviews WHERE recipe_id = ?", (recipe_a,)).fetchone():
            leftovers.append("recipe_reviews")
        if conn.execute("SELECT 1 FROM recipe_qc_results WHERE recipe_id = ?", (recipe_a,)).fetchone():
            leftovers.append("recipe_qc_results")
        if conn.execute(
            "SELECT 1 FROM recipe_relations WHERE recipe_id = ? OR related_recipe_id = ?",
            (recipe_a, recipe_a),
        ).fetchone():
            leftovers.append("recipe_relations")
        conn.close()

        if leftovers:
            return False, "deleting a recipe left rows behind in: " + ", ".join(leftovers)
        return True, None
    finally:
        # Runs whether the check passed, failed, or raised. Cleans up
        # recipe_b (the route under test never touches it) and, using
        # direct deletes rather than the route being tested, anything
        # still left of recipe_a if the delete didn't fully do its job.
        conn = sqlite3.connect("recipes.db")
        cursor = conn.cursor()
        for rid in (recipe_a, recipe_b):
            cursor.execute("DELETE FROM recipe_reviews WHERE recipe_id = ?", (rid,))
            cursor.execute("DELETE FROM recipe_qc_results WHERE recipe_id = ?", (rid,))
            cursor.execute("DELETE FROM recipe_relations WHERE recipe_id = ? OR related_recipe_id = ?", (rid, rid))
            cursor.execute("DELETE FROM recipes WHERE id = ?", (rid,))
        conn.commit()
        conn.close()


def check_clone_recipe_copies_correctly(client):
    """Creates a throwaway 'approved' fixture recipe with every column
    filled in (including the ones that only matter for this check --
    source_page_end/servings/prep_time/cook_time/notes/engine/
    extracted_at/flagged_for_review -- so nothing passes by accident just
    because it was already NULL), clones it through the real POST
    /recipes/{id}/clone route, and checks the new row against
    clone_recipe()'s documented contract in main.py: everything copied
    verbatim except title (CLONE-prefixed), status (forced to 'pending'
    even though the source here is 'approved' -- the whole point is that
    a clone never goes live with the template's own ingredients under it),
    and engine/extracted_at/flagged_for_review (cleared to NULL, the same
    state add_recipe() already leaves a hand-typed recipe in).

    Always cleans up both the original and the clone in a finally block,
    including any recipe_qc_results the clone route's QC re-run creates
    for the new row -- so it's safe to run against production."""
    headers = _admin_headers()
    if not headers:
        return True, "skipped -- ADMIN_EMAILS isn't set in this environment"

    marker = "__site_checks_clone_fixture__"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"

    conn = sqlite3.connect("recipes.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO recipes "
        "(title, source_book, source_path, source_page, source_page_end, ingredients, instructions, "
        " servings, prep_time, cook_time, notes, status, engine, extracted_at, flagged_for_review) "
        "VALUES (?, ?, 'fixture.pdf', 12, 13, 'ing', 'inst', '4', '10 min', '20 min', 'a note', "
        "'approved', 'ci-fixture', ?, 'a flag')",
        ("Fixture Original", marker, now),
    )
    original_id = cursor.lastrowid
    conn.commit()
    conn.close()

    new_id = None
    try:
        r = client.post(f"/recipes/{original_id}/clone", headers=headers, follow_redirects=False)
        if r.status_code != 303:
            return False, f"POST /recipes/{original_id}/clone returned {r.status_code}, expected 303"

        location = r.headers.get("location", "")
        parts = location.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "recipes" or parts[2] != "edit" or not parts[1].isdigit():
            return False, f"clone redirected to {location!r}, expected /recipes/<new id>/edit"
        new_id = int(parts[1])

        conn = sqlite3.connect("recipes.db")
        conn.row_factory = sqlite3.Row
        clone = conn.execute("SELECT * FROM recipes WHERE id = ?", (new_id,)).fetchone()
        conn.close()

        if clone is None:
            return False, f"clone route redirected to id {new_id}, but no such recipe was created"

        problems = []
        if clone["title"] != "CLONE Fixture Original":
            problems.append(f"title={clone['title']!r}, expected 'CLONE Fixture Original'")
        if clone["status"] != "pending":
            problems.append(f"status={clone['status']!r}, expected 'pending'")
        if clone["engine"] is not None:
            problems.append(f"engine={clone['engine']!r}, expected NULL")
        if clone["extracted_at"] is not None:
            problems.append(f"extracted_at={clone['extracted_at']!r}, expected NULL")
        if clone["flagged_for_review"] is not None:
            problems.append(f"flagged_for_review={clone['flagged_for_review']!r}, expected NULL")
        for col, expected in [
            ("source_book", marker), ("source_path", "fixture.pdf"), ("source_page", 12),
            ("source_page_end", 13), ("ingredients", "ing"), ("instructions", "inst"),
            ("servings", "4"), ("prep_time", "10 min"), ("cook_time", "20 min"), ("notes", "a note"),
        ]:
            if clone[col] != expected:
                problems.append(f"{col}={clone[col]!r}, expected {expected!r} (copied from the source)")

        if problems:
            return False, "cloned recipe doesn't match clone_recipe()'s contract: " + "; ".join(problems)
        return True, None
    finally:
        conn = sqlite3.connect("recipes.db")
        cursor = conn.cursor()
        for rid in (original_id, new_id):
            if rid is None:
                continue
            cursor.execute("DELETE FROM recipe_reviews WHERE recipe_id = ?", (rid,))
            cursor.execute("DELETE FROM recipe_qc_results WHERE recipe_id = ?", (rid,))
            cursor.execute("DELETE FROM recipe_relations WHERE recipe_id = ? OR related_recipe_id = ?", (rid, rid))
            cursor.execute("DELETE FROM recipes WHERE id = ?", (rid,))
        conn.commit()
        conn.close()


def check_edit_recipe_saves_source_page(client):
    """POSTs to the real /recipes/{id}/edit route with new source_page/
    source_page_end values and confirms both the database row and the
    recipe's own page (the "View original page" link's #page=... fragment)
    reflect the change. Incidentally the first CI coverage update_recipe()/
    edit_recipe() have ever had at all -- one UPDATE statement across every
    editable column (title/servings/prep_time/cook_time/ingredients/
    instructions/notes/flagged_for_review/source_page/source_page_end), so
    a typo in any one of those column names now fails this check with a
    clear sqlite3 error instead of only surfacing the next time someone
    saves an edit in production. Also the first CI coverage of GET
    /recipes/{id} itself, which is why ci_fixture_db.py's schema had to
    gain servings/prep_time/cook_time/notes/source_page_end alongside this
    check -- main.py's recipe_detail() route reads recipe["notes"]
    unconditionally, so that route would 500 against the old fixture.

    Always deletes the fixture recipe in a finally block, so it's safe to
    run against production."""
    headers = _admin_headers()
    if not headers:
        return True, "skipped -- ADMIN_EMAILS isn't set in this environment"

    marker = "__site_checks_edit_fixture__"
    conn = sqlite3.connect("recipes.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO recipes (title, source_book, source_path, source_page, ingredients, instructions, status) "
        "VALUES (?, ?, 'fixture.pdf', 1, 'ing', 'inst', 'pending')",
        (marker, marker),
    )
    recipe_id = cursor.lastrowid
    conn.commit()
    conn.close()

    try:
        r = client.post(
            f"/recipes/{recipe_id}/edit",
            headers=headers,
            data={
                "title": marker,
                "ingredients": "ing",
                "instructions": "inst",
                "source_page": "99",
                "source_page_end": "101",
            },
            follow_redirects=False,
        )
        if r.status_code != 303:
            return False, f"POST /recipes/{recipe_id}/edit returned {r.status_code}, expected 303"

        conn = sqlite3.connect("recipes.db")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT source_page, source_page_end FROM recipes WHERE id = ?", (recipe_id,)
        ).fetchone()
        conn.close()
        if row["source_page"] != 99 or row["source_page_end"] != 101:
            return False, (
                f"expected source_page=99/source_page_end=101 after editing, "
                f"got {row['source_page']}/{row['source_page_end']}"
            )

        detail = client.get(f"/recipes/{recipe_id}", headers=headers)
        if detail.status_code != 200:
            return False, f"GET /recipes/{recipe_id} returned {detail.status_code} after editing source_page"
        if "#page=99" not in detail.text:
            return False, "recipe page doesn't link to #page=99 after editing source_page"
        return True, None
    finally:
        conn = sqlite3.connect("recipes.db")
        cursor = conn.cursor()
        cursor.execute("DELETE FROM recipe_qc_results WHERE recipe_id = ?", (recipe_id,))
        cursor.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
        conn.commit()
        conn.close()


def check_qc_backfill_up_to_date(client):
    """Catches the exact "QC: 2/5" confusion from before the score was
    broadened -- a recipe checked before a new rule was added shows fewer
    recorded checks than CHECKS now has, until recipe_qc.py is re-run."""
    expected = len(_QC_CHECKS)
    conn = sqlite3.connect("recipes.db")
    stale = conn.execute("""
        SELECT r.id, COUNT(q.check_name) FROM recipes r
        LEFT JOIN recipe_qc_results q ON q.recipe_id = r.id
        WHERE r.status = 'approved'
        GROUP BY r.id
        HAVING COUNT(q.check_name) < ?
    """, (expected,)).fetchall()
    conn.close()
    if stale:
        ids = ", ".join(str(row[0]) for row in stale[:10]) + (", ..." if len(stale) > 10 else "")
        return False, (
            f"{len(stale)} approved recipe(s) have fewer than {expected} recorded QC checks "
            f"-- recipe_qc.py needs a backfill re-run (recipe ids: {ids})"
        )
    return True, None


def check_home_page_counts_match_database(client):
    conn = sqlite3.connect("recipes.db")
    recipe_count = conn.execute("SELECT COUNT(*) FROM recipes WHERE status = 'approved'").fetchone()[0]
    book_count = conn.execute("SELECT COUNT(DISTINCT source_book) FROM recipes WHERE status = 'approved'").fetchone()[0]
    conn.close()
    expected = (
        f"{recipe_count} recipe{'s' if recipe_count != 1 else ''} from "
        f"{book_count} book{'s' if book_count != 1 else ''}"
    )
    r = client.get("/")
    if expected not in r.text:
        return False, "home page stats line does not match the database (expected " + repr(expected) + ")"
    return True, None


def check_authors_page_lists_every_book(client):
    """Uses main.get_books_with_authors() -- the exact same resolution
    /authors itself calls -- rather than re-deriving an expected title by
    naively parsing source_book. That naive version flagged a false
    positive for any book whose title/author had since been manually
    corrected via /books' "Edit" button (see PROJECT_HISTORY.md,
    "Manually-set book title/author, and a precise /authors page"): the
    corrected title is what actually displays, not the original raw
    "Author - Title" parse, so checking for the latter failed even when
    the page was completely correct. Found 2026-09-28 when "Jamie Oliver -
    Jamies Dinners" (the raw parse) was flagged missing, even though the
    book was right there on the page as "Jamie's Dinner" (the corrected
    title).

    Compares against the HTML-escaped title, not the raw one -- Jinja2
    autoescapes template output, so a title with an apostrophe (like
    "Jamie's Dinner") actually renders as "Jamie&#39;s Dinner". Checking
    for the raw, unescaped title would have reintroduced a false positive
    for exactly the book that motivated this fix in the first place."""
    books = main.get_books_with_authors()
    r = client.get("/authors")
    missing = [b["title"] for b in books if str(_html_escape(b["title"])) not in r.text]
    if missing:
        return False, f"{len(missing)} approved book(s) missing from /authors: {', '.join(missing[:5])}"
    return True, None


CHECKS = [
    ("htmx_loaded_where_needed", check_htmx_loaded_where_needed),
    ("public_pages_load", check_public_pages_load),
    ("admin_pages_reject_anonymous", check_admin_pages_reject_anonymous),
    ("admin_pages_load_for_admin", check_admin_pages_load_for_admin),
    ("static_css_intact", check_static_css_intact),
    ("no_orphaned_foreign_keys", check_no_orphaned_foreign_keys),
    ("delete_recipe_cleans_up_related_rows", check_delete_recipe_cleans_up_related_rows),
    ("clone_recipe_copies_correctly", check_clone_recipe_copies_correctly),
    ("edit_recipe_saves_source_page", check_edit_recipe_saves_source_page),
    ("qc_backfill_up_to_date", check_qc_backfill_up_to_date),
    ("home_page_counts_match_database", check_home_page_counts_match_database),
    ("authors_page_lists_every_book", check_authors_page_lists_every_book),
]


def run_all():
    client = TestClient(main.app)
    results = []
    for name, func in CHECKS:
        try:
            passed, detail = func(client)
        except Exception as exc:
            passed, detail = False, f"check raised {type(exc).__name__}: {exc}"
        results.append({"name": name, "passed": passed, "detail": detail})
    return {
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
        "results": results,
    }


def save_results(summary):
    RESULTS_PATH.write_text(json.dumps(summary, indent=2))


def load_results():
    if not RESULTS_PATH.exists():
        return None
    return json.loads(RESULTS_PATH.read_text())


def _cli():
    ap = argparse.ArgumentParser(description="Outer-loop regression checks for the recipe-app web app")
    ap.add_argument("--quiet", action="store_true", help="only print output when something fails (for cron)")
    args = ap.parse_args()

    summary = run_all()
    save_results(summary)

    failed = [r for r in summary["results"] if not r["passed"]]
    if not args.quiet or failed:
        for r in summary["results"]:
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"[{mark}] {r['name']}" + (f" -- {r['detail']}" if r["detail"] else ""))
        print()
        print(f"{len(summary['results']) - len(failed)}/{len(summary['results'])} unit tests passed")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _cli()
