import os
import re
import sqlite3
from fastapi import FastAPI, Request, Form, Response, HTTPException, Depends

from pipeline.recipe_qc import init_qc_table as _init_recipe_qc_table, CHECKS as _QC_CHECKS, run_checks as _run_recipe_qc_checks
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, FileResponse

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Extracted titles come out however the source book (or the model) styled
# them -- some in ALL CAPS, some all lowercase, some already fine -- and
# it's not worth normalizing that in the database, since the raw extracted
# value is still useful to see when editing/reviewing. Instead, title-case
# it for display only, everywhere a title is shown to a viewer.
_MINOR_WORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in",
    "into", "nor", "of", "on", "onto", "or", "over", "per", "so",
    "the", "to", "up", "via", "with", "yet",
}
_FIRST_LETTER_RE = re.compile(r"[A-Za-z]")


def _capitalize_word(word: str) -> str:
    """Uppercases just the first letter of each hyphen-separated part of a
    word, leaving everything else untouched. That's what makes this safe
    on apostrophes -- "witches'" becomes "Witches'", never "Witches'S" the
    way Python's built-in str.title() would mangle a word like "don't"
    into "Don'T" -- we only ever touch the very first letter of each part,
    nothing after an apostrophe."""
    parts = word.split("-")
    capitalized = []
    for part in parts:
        m = _FIRST_LETTER_RE.search(part)
        if m:
            i = m.start()
            part = part[:i] + part[i].upper() + part[i + 1:]
        capitalized.append(part)
    return "-".join(capitalized)


def smart_title_case(title: str) -> str:
    """Title-cases a recipe title for display, regardless of how it's
    capitalized in the database (ALL CAPS from an OCR'd heading, all
    lowercase from a model that didn't bother, whatever). Lowercases
    everything first so the input casing can't interfere, then capitalizes
    each "major" word -- short connecting words (a, and, of, with, ...)
    stay lowercase unless they're the first or last word, same convention
    as a book's own chapter headings."""
    if not title:
        return title
    words = title.lower().split()
    if not words:
        return title
    last_index = len(words) - 1
    result = []
    for i, word in enumerate(words):
        core = word.strip(".,;:!?()[]{}\"'")
        if core in _MINOR_WORDS and 0 < i < last_index:
            result.append(word)
        else:
            result.append(_capitalize_word(word))
    return " ".join(result)


templates.env.filters["titlecase"] = smart_title_case

INVENTORY_DB_PATH = os.path.expanduser("~/cookbook-project/inventory.sqlite")

# Cloudflare Access already authenticates every visitor (email allowlist +
# one-time-pin login) before a request ever reaches this app, and it passes
# the verified email through in this header. Trusting it isn't an absolute
# guarantee here: uvicorn binds to 0.0.0.0, not 127.0.0.1 (deliberate --
# see documentation/ARCHITECTURE.md "Admin access control" for why), so the
# app is also reachable directly on the home LAN, not only through the
# Cloudflare Tunnel. A device on the LAN that specifically crafts this
# header itself could pose as an admin; ordinary browsing from a LAN device
# does not (browsers don't send this header on their own). Accepted as-is
# for this home network -- see ARCHITECTURE.md for the full reasoning and
# the fix if that risk profile ever changes. ADMIN_EMAILS is read from the
# environment (not hardcoded) since this repo is public -- set it wherever
# you start the app, e.g.:
#   export ADMIN_EMAILS="you@example.com"
# If it's unset, is_admin() is False for everyone, including you -- that's
# a deliberate fail-closed default, not a bug.
ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("ADMIN_EMAILS", "").split(",")
    if e.strip()
}


def is_admin(request: Request) -> bool:
    email = (request.headers.get("Cf-Access-Authenticated-User-Email") or "").strip().lower()
    return bool(email) and email in ADMIN_EMAILS


def require_admin(request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="Admins only")


def current_user_email(request: Request) -> str | None:
    """The verified email Cloudflare Access attaches to every request (see
    the comment on ADMIN_EMAILS above for the trust model), or None if it's
    missing. Unlike is_admin(), this doesn't gate anything -- every visitor
    who reaches the site gets one -- it's just how we attribute a "made it"
    tick or a review to a real person without building our own login."""
    email = (request.headers.get("Cf-Access-Authenticated-User-Email") or "").strip().lower()
    return email or None


def init_recipe_reviews_table():
    conn = sqlite3.connect("recipes.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recipe_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL,
            user_email TEXT NOT NULL,
            made_it INTEGER NOT NULL DEFAULT 0,
            rating INTEGER,
            review_text TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(recipe_id, user_email)
        )
    """)
    conn.commit()
    conn.close()


init_recipe_reviews_table()


def init_recipes_extracted_column():
    """Adds a manual per-book tracking column to inventory.sqlite so a book
    can be ticked off once its recipes have been extracted and reviewed.
    Deliberately NOT auto-detected from recipes.db (which run, which engine,
    which --pages-per-chunk a book was processed with all vary), so this is
    a plain admin-set flag rather than a computed status -- same spirit as
    the existing 'excluded' column."""
    conn = sqlite3.connect(INVENTORY_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(inventory)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    if "recipes_extracted" not in existing_columns:
        conn.execute("ALTER TABLE inventory ADD COLUMN recipes_extracted INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    conn.close()


init_recipes_extracted_column()


def init_book_title_author_columns():
    """Adds manually-settable 'title' and 'author' columns to inventory.sqlite,
    so a book's display name doesn't have to rely on its (often messy,
    '...toOCR'-suffixed) filename. NULL means "not set yet -- fall back to
    the filename (and to parsing recipes.source_book for the author)", same
    spirit as 'excluded' and 'recipes_extracted': a plain admin-set field,
    never a computed one."""
    conn = sqlite3.connect(INVENTORY_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(inventory)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    if "title" not in existing_columns:
        conn.execute("ALTER TABLE inventory ADD COLUMN title TEXT")
    if "author" not in existing_columns:
        conn.execute("ALTER TABLE inventory ADD COLUMN author TEXT")
    conn.commit()
    conn.close()


init_book_title_author_columns()


def init_recipe_qc_table():
    """Same table the pipeline's recipe_qc.py creates -- initialized here
    too so the review queue and /qc-issues pages don't fail on a fresh
    recipes.db that hasn't had a pipeline run (or a QC backfill) against
    it yet."""
    conn = sqlite3.connect("recipes.db")
    _init_recipe_qc_table(conn)
    conn.close()


init_recipe_qc_table()


def init_recipe_relations_table():
    """Links between recipes -- 'needs' (this recipe is built from, or
    requires, another already in the library, e.g. a puttanesca needing a
    base tomato sauce) and 'pairs_with' (a looser 'goes well together'
    link, e.g. a sauce and the pasta it's usually served with). Each row
    is authored by one recipe (recipe_id) pointing at another
    (related_recipe_id); 'pairs_with' is treated as mutual at display
    time by reading both directions, so it only needs entering once."""
    conn = sqlite3.connect("recipes.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recipe_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL,
            related_recipe_id INTEGER NOT NULL,
            relation_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(recipe_id, related_recipe_id, relation_type)
        )
    """)
    conn.commit()
    conn.close()


init_recipe_relations_table()


def init_feedback_table():
    """Bug reports and feature ideas submitted through /feedback by anyone
    who reaches the site (see current_user_email()'s docstring on the
    trust model -- same idea: attribute it to a real person via Cloudflare
    Access, without building a login system). status starts at 'new' and
    is moved to 'done' or 'dismissed' from the admin-only /feedback/inbox
    page as items get triaged."""
    conn = sqlite3.connect("recipes.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS site_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT,
            kind TEXT NOT NULL,
            message TEXT NOT NULL,
            page_url TEXT,
            status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


init_feedback_table()


def get_recipes_for_book(source_book, exclude_id=None):
    """Every recipe sharing source_book with the recipe being edited (used
    to populate the Needs / Works well with dropdowns) -- deliberately
    scoped to the same book, since a cross-book reference to a recipe
    that might get renamed, re-extracted, or never approved is a lot more
    fragile than one within a single cookbook."""
    conn = sqlite3.connect("recipes.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, title FROM recipes WHERE source_book IS ? AND id != ? ORDER BY title",
        (source_book, exclude_id),
    )
    recipes = cursor.fetchall()
    conn.close()
    return recipes


def get_authored_relation_ids(recipe_id, relation_type):
    """The set of related_recipe_id values this recipe itself has
    authored for one relation_type -- used to pre-select the edit form's
    dropdowns with exactly what a save would reproduce (as opposed to
    get_recipe_relations' merged, bidirectional view used for display)."""
    conn = sqlite3.connect("recipes.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT related_recipe_id FROM recipe_relations WHERE recipe_id = ? AND relation_type = ?",
        (recipe_id, relation_type),
    )
    ids = {row[0] for row in cursor.fetchall()}
    conn.close()
    return ids


def get_recipe_relations(recipe_id):
    """The relations to show on a recipe's own page: recipes it needs,
    recipes that need IT (the reverse of 'needs' -- e.g. viewing a base
    tomato sauce shows every recipe built on top of it), and recipes it
    pairs with (merged from both directions, since a pairing is mutual
    regardless of which recipe's edit form it was entered on)."""
    conn = sqlite3.connect("recipes.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    needs = cursor.execute("""
        SELECT r.id, r.title FROM recipe_relations rel
        JOIN recipes r ON r.id = rel.related_recipe_id
        WHERE rel.recipe_id = ? AND rel.relation_type = 'needs'
        ORDER BY r.title
    """, (recipe_id,)).fetchall()

    used_as_base_for = cursor.execute("""
        SELECT r.id, r.title FROM recipe_relations rel
        JOIN recipes r ON r.id = rel.recipe_id
        WHERE rel.related_recipe_id = ? AND rel.relation_type = 'needs'
        ORDER BY r.title
    """, (recipe_id,)).fetchall()

    pairs_with = cursor.execute("""
        SELECT r.id, r.title FROM recipe_relations rel
        JOIN recipes r ON r.id = rel.related_recipe_id
        WHERE rel.recipe_id = ? AND rel.relation_type = 'pairs_with'
        UNION
        SELECT r.id, r.title FROM recipe_relations rel
        JOIN recipes r ON r.id = rel.recipe_id
        WHERE rel.related_recipe_id = ? AND rel.relation_type = 'pairs_with'
        ORDER BY title
    """, (recipe_id, recipe_id)).fetchall()

    conn.close()
    return {"needs": needs, "used_as_base_for": used_as_base_for, "pairs_with": pairs_with}


def set_recipe_relations(recipe_id, needs_ids, pairs_with_ids):
    """Replaces every relation authored BY this recipe (recipe_id = this)
    with exactly what was submitted -- relations authored by other
    recipes pointing at this one (e.g. another recipe's 'needs' entry, or
    the reverse side of a mutual pairing) are untouched, since they don't
    belong to this recipe's row."""
    conn = sqlite3.connect("recipes.db")
    conn.execute("DELETE FROM recipe_relations WHERE recipe_id = ?", (recipe_id,))
    for rid in needs_ids:
        if rid != recipe_id:
            conn.execute(
                "INSERT OR IGNORE INTO recipe_relations (recipe_id, related_recipe_id, relation_type, created_at) VALUES (?, ?, 'needs', datetime('now'))",
                (recipe_id, rid),
            )
    for rid in pairs_with_ids:
        if rid != recipe_id:
            conn.execute(
                "INSERT OR IGNORE INTO recipe_relations (recipe_id, related_recipe_id, relation_type, created_at) VALUES (?, ?, 'pairs_with', datetime('now'))",
                (recipe_id, rid),
            )
    conn.commit()
    conn.close()


def _attach_qc_info(conn, recipes: list[dict]) -> list[dict]:
    """Fetches every recorded QC check for the given recipes (as plain
    dicts, id keyed) and annotates each with qc_total/qc_passed (a quick
    "5/7 checks passed" summary, across every check -- hard and advisory
    alike) and qc_failed (the individual still-failing checks, with their
    detail text) -- used by both the pending review queue and the
    /qc-issues page. A failed ADVISORY check that's been manually
    acknowledged (the "Reviewed, OK" checkbox on the recipe page) counts
    as passed here and is left out of qc_failed, same as a check that
    never failed in the first place."""
    if not recipes:
        return recipes
    ids = [r["id"] for r in recipes]
    placeholders = ",".join("?" * len(ids))
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT recipe_id, check_name, severity, passed, detail, acknowledged FROM recipe_qc_results WHERE recipe_id IN ({placeholders})",
        ids,
    )
    by_recipe: dict = {}
    for row in cursor.fetchall():
        by_recipe.setdefault(row["recipe_id"], []).append(dict(row))

    for r in recipes:
        checks = by_recipe.get(r["id"], [])
        r["qc_total"] = len(checks)
        r["qc_passed"] = sum(1 for c in checks if c["passed"] or c["acknowledged"])
        r["qc_failed"] = [c for c in checks if not c["passed"] and not c["acknowledged"]]
    return recipes


def get_recipes(search_term: str = "", book_filter: str = ""):
    conn = sqlite3.connect("recipes.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    query = "SELECT id, title, source_book FROM recipes WHERE status = 'approved'"
    params = []
    if search_term:
        query += " AND title LIKE ?"
        params.append(f"%{search_term}%")
    if book_filter:
        query += " AND source_book = ?"
        params.append(book_filter)
    cursor.execute(query, params)
    recipes = cursor.fetchall()
    conn.close()
    return recipes


def get_distinct_source_books():
    """Books that actually have at least one approved (visible) recipe --
    used to populate the 'filter by book' dropdown on the home page. Not the
    same list as the /books library, which covers the whole ~700-book
    pipeline regardless of extraction status."""
    conn = sqlite3.connect("recipes.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT DISTINCT source_book FROM recipes WHERE status = 'approved' ORDER BY source_book COLLATE NOCASE"
    )
    books = [row[0] for row in cursor.fetchall()]
    conn.close()
    return books


def _build_inventory_title_author_lookup():
    """Maps every path form a recipe's source_path might take (the original
    inventory path, the OCR output path, or the compressed output path --
    extract_recipes.py accepts any of the three as --pdf, so there's no one
    fixed form) back to that book's manually-set (title, author) from
    inventory.sqlite, a completely separate database from recipes.db (see
    "The two databases" in ARCHITECTURE.md). Built once per call rather
    than once per book -- ~700 rows, cheap either way -- so
    get_books_with_authors() isn't doing a fresh cross-database query per
    group."""
    conn = sqlite3.connect(INVENTORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT i.path, i.title, i.author,
               o.output_path AS ocr_output_path,
               c.output_path AS compressed_output_path
        FROM inventory i
        LEFT JOIN ocr_results o ON i.path = o.path
        LEFT JOIN compress_results c ON c.path = COALESCE(o.output_path, i.path)
    """)
    lookup = {}
    for row in cursor.fetchall():
        info = {"title": row["title"], "author": row["author"]}
        for path in (row["path"], row["ocr_output_path"], row["compressed_output_path"]):
            if path:
                lookup[path] = info
    conn.close()
    return lookup


def get_books_with_authors():
    """Approved recipes' distinct source books, resolved to a (author,
    title) pair for the reader-facing browse-by-author page. Prefers the
    manually-set title/author from inventory.sqlite (see /books' "Edit"
    button) when the recipe's source_path can be traced back to an
    inventory row; falls back to parsing source_book's "Author - Title"
    convention (see the --book-title flag in extract_recipes.py /
    extract_recipes_local.py) per field when it can't -- older books, or
    ones without a title/author set yet, still display exactly as before.
    A source_book with no " - " separator and no inventory match falls
    back to author "Unknown" rather than failing to display it."""
    conn = sqlite3.connect("recipes.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT source_book, MIN(source_path), COUNT(*)
        FROM recipes WHERE status = 'approved' GROUP BY source_book
    """)
    rows = cursor.fetchall()
    conn.close()

    inventory_lookup = _build_inventory_title_author_lookup()

    books = []
    for source_book, source_path, recipe_count in rows:
        if " - " in source_book:
            parsed_author, parsed_title = source_book.split(" - ", 1)
        else:
            parsed_author, parsed_title = "Unknown", source_book
        inventory_info = inventory_lookup.get(source_path) if source_path else None
        if inventory_info:
            author = inventory_info["author"] or parsed_author
            title = inventory_info["title"] or parsed_title
        else:
            author, title = parsed_author, parsed_title
        books.append({
            "author": author,
            "title": title,
            "source_book": source_book,
            "recipe_count": recipe_count,
        })
    books.sort(key=lambda b: (b["author"].lower(), b["title"].lower()))
    return books


def get_recipe_by_id(recipe_id: int):
    conn = sqlite3.connect("recipes.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,))
    recipe = cursor.fetchone()
    conn.close()
    return recipe


def _sort_pending_recipes(recipes, sort):
    """Reorders the pending-review list per the requested sort. 'book_asc'
    (the default) is already how the underlying query orders rows, so
    it's a no-op; the others re-sort in place, relying on Python's stable
    sort to keep the existing source_book/id ordering as a tiebreak.
    Recipes with no QC checks recorded yet always sort last under a
    QC-score sort, and recipes with no extracted_at (e.g. added by hand,
    not through the extraction pipeline) always sort last under a date
    sort, in either direction, since there's nothing to rank them by."""
    if sort == "book_desc":
        recipes.sort(key=lambda r: (r["source_book"] or "").lower(), reverse=True)
    elif sort in ("qc_asc", "qc_desc"):
        def qc_key(r):
            total = r.get("qc_total") or 0
            if not total:
                return (1, 0)
            score = r["qc_passed"] / total
            return (0, -score if sort == "qc_desc" else score)
        recipes.sort(key=qc_key)
    elif sort in ("date_asc", "date_desc"):
        # extracted_at is stored as an ISO-ish "YYYY-MM-DD HH:MM:SS" string,
        # so plain string comparison already sorts it chronologically --
        # no need to parse it into a datetime first.
        dated = [r for r in recipes if r.get("extracted_at")]
        undated = [r for r in recipes if not r.get("extracted_at")]
        dated.sort(key=lambda r: r["extracted_at"], reverse=(sort == "date_desc"))
        recipes[:] = dated + undated
    return recipes


def _qc_score_str(recipe: dict) -> str:
    """The exact "passed/total" string a recipe's QC badge displays, or
    'none' for a recipe with no QC checks recorded yet -- used both to
    build the review queue's QC-score filter options and to match a
    recipe against the selected one, so a filter option always means
    exactly what it says on the badge."""
    total = recipe.get("qc_total") or 0
    if not total:
        return "none"
    return f"{recipe['qc_passed']}/{total}"


def _extracted_date_str(recipe: dict) -> str:
    """The DAY (not time) a recipe was extracted, or 'none' if it has no
    extracted_at at all (e.g. added by hand rather than through the
    pipeline) -- same sentinel convention as _qc_score_str."""
    extracted_at = recipe.get("extracted_at")
    return extracted_at[:10] if extracted_at else "none"


def _pending_recipe_filter_options(recipes: list[dict]) -> dict:
    """The book/QC-score/date choices to offer on the review queue's
    filter row, computed from the FULL pending set before any filter is
    applied -- so the dropdowns always list every available option,
    rather than narrowing themselves as filters get chosen. QC scores are
    sorted highest-total-first then highest-passed-first (7/7 before
    6/7 before 6/6), dates newest first; 'no QC data yet' / 'no
    extraction date' are appended last in each, only when at least one
    recipe actually needs them."""
    books = sorted({r["source_book"] for r in recipes if r["source_book"]}, key=str.lower)

    qc_scores = sorted(
        {(r["qc_passed"], r["qc_total"]) for r in recipes if r.get("qc_total")},
        key=lambda t: (-t[1], -t[0]),
    )
    has_no_qc = any(not r.get("qc_total") for r in recipes)

    dates = sorted({d for r in recipes if (d := _extracted_date_str(r)) != "none"}, reverse=True)
    has_no_date = any(_extracted_date_str(r) == "none" for r in recipes)

    return {
        "books": books,
        "qc_scores": [f"{passed}/{total}" for passed, total in qc_scores],
        "has_no_qc": has_no_qc,
        "dates": dates,
        "has_no_date": has_no_date,
    }


def get_pending_recipes(sort: str = "book_asc", book_filter: str = "", qc_filter: str = "", date_filter: str = ""):
    conn = sqlite3.connect("recipes.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, title, source_book, engine, flagged_for_review, extracted_at
        FROM recipes
        WHERE status != 'approved'
        ORDER BY source_book, id
    """)
    recipes = [dict(row) for row in cursor.fetchall()]
    recipes = _attach_qc_info(conn, recipes)
    conn.close()

    filter_options = _pending_recipe_filter_options(recipes)

    if book_filter:
        recipes = [r for r in recipes if r["source_book"] == book_filter]
    if qc_filter:
        recipes = [r for r in recipes if _qc_score_str(r) == qc_filter]
    if date_filter:
        recipes = [r for r in recipes if _extracted_date_str(r) == date_filter]

    return _sort_pending_recipes(recipes, sort), filter_options


def get_qc_failed_recipes():
    """Recipes -- approved or not -- that failed at least one HARD QC
    check. This is the backfill-discovered counterpart to the pending
    review queue: a check added, or backfilled with recipe_qc.py, after a
    recipe was already approved has nowhere else to surface, since the
    review queue only shows status != 'approved'."""
    conn = sqlite3.connect("recipes.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT r.id, r.title, r.source_book, r.engine, r.status
        FROM recipes r
        JOIN recipe_qc_results q ON q.recipe_id = r.id
        WHERE q.severity = 'hard' AND q.passed = 0
        ORDER BY r.source_book, r.id
    """)
    recipes = [dict(row) for row in cursor.fetchall()]
    recipes = _attach_qc_info(conn, recipes)
    conn.close()
    return recipes


def get_qc_results_for_recipe(recipe_id: int):
    """Every recorded QC check for one recipe -- passes included, unlike
    _attach_qc_info's qc_failed -- for the full pass/fail table on the
    recipe detail page. Returned in the same order as CHECKS so the table
    doesn't reshuffle between recipes or page loads."""
    conn = sqlite3.connect("recipes.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT check_name, severity, passed, detail, acknowledged FROM recipe_qc_results WHERE recipe_id = ?",
        (recipe_id,),
    )
    by_name = {row["check_name"]: dict(row) for row in cursor.fetchall()}
    conn.close()
    return [by_name[name] for name, _, _ in _QC_CHECKS if name in by_name]


def create_recipe(title, source_book, ingredients, instructions):
    conn = sqlite3.connect("recipes.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO recipes (title, source_book, ingredients, instructions) VALUES (?, ?, ?, ?)",
        (title, source_book, ingredients, instructions)
    )
    conn.commit()
    conn.close()


def update_recipe(recipe_id, title, servings, prep_time, cook_time, ingredients, instructions, notes, flagged_for_review):
    conn = sqlite3.connect("recipes.db")
    conn.execute("""
        UPDATE recipes
        SET title = ?, servings = ?, prep_time = ?, cook_time = ?,
            ingredients = ?, instructions = ?, notes = ?, flagged_for_review = ?
        WHERE id = ?
    """, (
        title,
        servings or None,
        prep_time or None,
        cook_time or None,
        ingredients,
        instructions,
        notes or None,
        flagged_for_review or None,
        recipe_id,
    ))
    conn.commit()
    conn.close()

def get_reviews_for_recipe(recipe_id: int):
    conn = sqlite3.connect("recipes.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_email, made_it, rating, review_text, updated_at
        FROM recipe_reviews
        WHERE recipe_id = ?
        ORDER BY updated_at DESC
    """, (recipe_id,))
    reviews = cursor.fetchall()
    conn.close()
    return reviews


def get_user_review(recipe_id: int, user_email: str):
    conn = sqlite3.connect("recipes.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT made_it, rating, review_text FROM recipe_reviews WHERE recipe_id = ? AND user_email = ?",
        (recipe_id, user_email),
    )
    review = cursor.fetchone()
    conn.close()
    return review


def upsert_review(recipe_id: int, user_email: str, made_it: bool, rating: int | None, review_text: str | None):
    conn = sqlite3.connect("recipes.db")
    conn.execute("""
        INSERT INTO recipe_reviews (recipe_id, user_email, made_it, rating, review_text, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(recipe_id, user_email) DO UPDATE SET
            made_it = excluded.made_it,
            rating = excluded.rating,
            review_text = excluded.review_text,
            updated_at = excluded.updated_at
    """, (recipe_id, user_email, int(made_it), rating, review_text))
    conn.commit()
    conn.close()


def get_books(search_term: str = "", status_filter: str = "", recipes_filter: str = "", sort: str = "filename_asc", show_hidden: bool = False):
    conn = sqlite3.connect(INVENTORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    base_query = """
        SELECT * FROM (
            SELECT
                i.path, i.filename, i.excluded, i.recipes_extracted,
                i.title, i.author,
                CASE
                    WHEN c.status = 'verified_ok' THEN c.compressed_bytes
                    ELSE i.size_bytes
                END AS size_bytes,
                MAX(
                    COALESCE(i.scanned_at, ''),
                    COALESCE(o.completed_at, ''),
                    COALESCE(c.completed_at, '')
                ) AS last_updated,
                CASE
                    WHEN i.status = 'unsupported_extension' THEN 'unsupported_extension'
                    WHEN c.status = 'verified_ok' THEN 'compressed'
                    WHEN o.ocr_status LIKE 'ocr_success%' THEN 'ocr_done'
                    WHEN i.status = 'native_force_reocred' THEN 'ocr_done'
                    WHEN i.status = 'readable_native' THEN 'readable_native'
                    ELSE 'needs_ocr'
                END AS status
            FROM inventory i
            LEFT JOIN ocr_results o ON i.path = o.path
            LEFT JOIN compress_results c ON c.path = COALESCE(o.output_path, i.path)
        )
        WHERE 1=1
    """
    params = []
    if not show_hidden:
        base_query += " AND excluded = 0"
    if status_filter:
        base_query += " AND status = ?"
        params.append(status_filter)
    else:
        base_query += " AND status != 'unsupported_extension'"
    if recipes_filter == "done":
        base_query += " AND recipes_extracted = 1"
    elif recipes_filter == "not_done":
        base_query += " AND recipes_extracted = 0"
    if search_term:
        base_query += " AND filename LIKE ?"
        params.append(f"%{search_term}%")

    sort_columns = {
        "filename_asc": "filename ASC",
        "filename_desc": "filename DESC",
        "status_asc": "status ASC",
        "status_desc": "status DESC",
        "size_asc": "size_bytes ASC",
        "size_desc": "size_bytes DESC",
        "scanned_at_asc": "last_updated ASC",
        "scanned_at_desc": "last_updated DESC",
    }
    base_query += " ORDER BY " + sort_columns.get(sort, "filename ASC")

    cursor.execute(base_query, params)
    books = cursor.fetchall()
    conn.close()
    return books


def get_book_title_author(path: str):
    """One inventory row's path/filename/title/author, for the edit-form
    and display partials on /books. Deliberately a plain, un-joined lookup
    -- those partials don't need status/size, just enough to render the
    "title (falls back to filename) + author" display and pre-fill the
    edit form."""
    conn = sqlite3.connect(INVENTORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT path, filename, title, author FROM inventory WHERE path = ?", (path,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_book_status_counts(show_hidden: bool = False):
    conn = sqlite3.connect(INVENTORY_DB_PATH)
    cursor = conn.cursor()
    query = """
        SELECT status, COUNT(*) FROM (
            SELECT
                i.excluded,
                CASE
                    WHEN i.status = 'unsupported_extension' THEN 'unsupported_extension'
                    WHEN c.status = 'verified_ok' THEN 'compressed'
                    WHEN o.ocr_status LIKE 'ocr_success%' THEN 'ocr_done'
                    WHEN i.status = 'native_force_reocred' THEN 'ocr_done'
                    WHEN i.status = 'readable_native' THEN 'readable_native'
                    ELSE 'needs_ocr'
                END AS status
            FROM inventory i
            LEFT JOIN ocr_results o ON i.path = o.path
            LEFT JOIN compress_results c ON c.path = COALESCE(o.output_path, i.path)
        )
    """
    if not show_hidden:
        query += " WHERE excluded = 0"
    query += " GROUP BY status ORDER BY COUNT(*) DESC"
    cursor.execute(query)
    counts = cursor.fetchall()
    conn.close()
    return counts


def get_recipe_extraction_counts(show_hidden: bool = False):
    """How many (non-excluded, by default) books have been ticked off as
    'recipes extracted' vs. not -- the progress-tracking summary for the
    library page."""
    conn = sqlite3.connect(INVENTORY_DB_PATH)
    cursor = conn.cursor()
    query = "SELECT recipes_extracted, COUNT(*) FROM inventory"
    if not show_hidden:
        query += " WHERE excluded = 0"
    query += " GROUP BY recipes_extracted"
    cursor.execute(query)
    rows = dict(cursor.fetchall())
    conn.close()
    return {"done": rows.get(1, 0), "not_done": rows.get(0, 0)}

def get_pending_recipe_count():
    """Just the count, for the public-facing teaser on the home page --
    no need to pull every pending recipe's title/QC info (like
    get_pending_recipes() does for the review queue itself) just to show
    a number to a non-admin visitor."""
    conn = sqlite3.connect("recipes.db")
    count = conn.execute("SELECT COUNT(*) FROM recipes WHERE status != 'approved'").fetchone()[0]
    conn.close()
    return count


def get_feedback():
    """Every feedback submission, newest first -- unresolved ('new') items
    sorted ahead of triaged ones ('done'/'dismissed'), so the inbox always
    shows what still needs a look at the top rather than making you scroll
    past resolved items sorted purely by date."""
    conn = sqlite3.connect("recipes.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, user_email, kind, message, page_url, status, created_at
        FROM site_feedback
        ORDER BY (status = 'new') DESC, created_at DESC
    """)
    items = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return items


def get_open_feedback_count():
    conn = sqlite3.connect("recipes.db")
    count = conn.execute("SELECT COUNT(*) FROM site_feedback WHERE status = 'new'").fetchone()[0]
    conn.close()
    return count


@app.get("/")
def read_root(request: Request, book: str = ""):
    recipes = get_recipes(book_filter=book)
    books = get_distinct_source_books()
    return templates.TemplateResponse(
        request=request, name="home.html",
        context={
            "recipes": recipes, "books": books, "book": book,
            "is_admin": is_admin(request),
            "pending_count": get_pending_recipe_count(),
        }
    )


@app.get("/search")
def search(request: Request, q: str = "", book: str = ""):
    recipes = get_recipes(q, book)
    return templates.TemplateResponse(
        request=request, name="recipe_list.html", context={"recipes": recipes, "is_admin": is_admin(request)}
    )


@app.get("/authors")
def authors_page(request: Request):
    books = get_books_with_authors()
    return templates.TemplateResponse(
        request=request, name="authors.html",
        context={"books": books, "is_admin": is_admin(request)}
    )

@app.get("/feedback")
def feedback_form(request: Request, submitted: str = ""):
    return templates.TemplateResponse(
        request=request, name="feedback.html",
        context={
            "is_admin": is_admin(request),
            "submitted": bool(submitted),
            "referer": request.headers.get("referer", ""),
        }
    )


@app.post("/feedback")
def submit_feedback(request: Request, kind: str = Form(...), message: str = Form(...), page_url: str = Form("")):
    user_email = current_user_email(request)
    if not user_email:
        # Same trust model as recipe reviews (see current_user_email()'s
        # docstring) -- fail loudly rather than saving feedback with no
        # idea who it's from.
        raise HTTPException(status_code=400, detail="Couldn't identify who you are -- are you accessing this through Cloudflare Access?")
    message = message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Feedback message can't be empty")
    if kind not in ("bug", "feature"):
        kind = "feature"
    conn = sqlite3.connect("recipes.db")
    conn.execute("""
        INSERT INTO site_feedback (user_email, kind, message, page_url, status, created_at)
        VALUES (?, ?, ?, ?, 'new', datetime('now'))
    """, (user_email, kind, message, page_url.strip() or None))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/feedback?submitted=1", status_code=303)


@app.get("/review")
def review_page(
    request: Request, sort: str = "book_asc", book: str = "", qc: str = "", date: str = "",
    _: None = Depends(require_admin),
):
    recipes, filter_options = get_pending_recipes(sort=sort, book_filter=book, qc_filter=qc, date_filter=date)
    return templates.TemplateResponse(
        request=request, name="review.html",
        context={
            "recipes": recipes, "is_admin": True, "sort": sort,
            "book_filter": book, "qc_filter": qc, "date_filter": date,
            "filter_options": filter_options,
            "total_pending_count": get_pending_recipe_count(),
        }
    )

@app.get("/qc-issues")
def qc_issues_page(request: Request, _: None = Depends(require_admin)):
    recipes = get_qc_failed_recipes()
    return templates.TemplateResponse(
        request=request, name="qc_issues.html", context={"recipes": recipes, "is_admin": True}
    )


@app.get("/unit-tests")
def unit_tests_page(request: Request, _: None = Depends(require_admin)):
    # Imported here rather than at module load time: site_checks.py imports
    # this module back (it needs the FastAPI app to test it), so importing
    # it up front would be a circular import at server startup. By the time
    # a request actually reaches this route, main.py has long finished
    # loading, so there's nothing circular about it any more.
    import site_checks
    summary = site_checks.load_results()
    if summary is None:
        return templates.TemplateResponse(
            request=request, name="unit_tests.html",
            context={"results": None, "is_admin": True}
        )
    results = summary["results"]
    passed_count = sum(1 for r in results if r["passed"])
    return templates.TemplateResponse(
        request=request, name="unit_tests.html",
        context={
            "results": results, "ran_at": summary["ran_at"],
            "passed_count": passed_count, "total_count": len(results),
            "failed_count": len(results) - passed_count, "is_admin": True,
        }
    )


@app.post("/unit-tests/run")
def run_unit_tests(_: None = Depends(require_admin)):
    import site_checks
    summary = site_checks.run_all()
    site_checks.save_results(summary)
    return RedirectResponse(url="/unit-tests", status_code=303)


@app.get("/feedback/inbox")
def feedback_inbox(request: Request, _: None = Depends(require_admin)):
    items = get_feedback()
    open_count = get_open_feedback_count()
    return templates.TemplateResponse(
        request=request, name="feedback_inbox.html",
        context={"items": items, "is_admin": True, "open_count": open_count}
    )


@app.post("/feedback/{feedback_id}/status")
def update_feedback_status(feedback_id: int, status: str = Form(...), _: None = Depends(require_admin)):
    if status not in ("new", "done", "dismissed"):
        raise HTTPException(status_code=400, detail="Invalid status")
    conn = sqlite3.connect("recipes.db")
    conn.execute("UPDATE site_feedback SET status = ? WHERE id = ?", (status, feedback_id))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/feedback/inbox", status_code=303)

@app.get("/books")
def books_page(request: Request, sort: str = "filename_asc", show_hidden: bool = False):
    books = get_books(sort=sort, show_hidden=show_hidden)
    counts = get_book_status_counts(show_hidden=show_hidden)
    recipe_counts = get_recipe_extraction_counts(show_hidden=show_hidden)
    return templates.TemplateResponse(
        request=request, name="books.html",
        context={"books": books, "counts": counts, "recipe_counts": recipe_counts, "sort": sort, "show_hidden": show_hidden, "is_admin": is_admin(request)}
    )

@app.get("/books/search")
def books_search(request: Request, q: str = "", status: str = "", recipes_filter: str = "", sort: str = "filename_asc", show_hidden: bool = False):
    books = get_books(search_term=q, status_filter=status, recipes_filter=recipes_filter, sort=sort, show_hidden=show_hidden)
    return templates.TemplateResponse(
        request=request, name="books_list.html",
        context={"books": books, "sort": sort, "show_hidden": show_hidden, "is_admin": is_admin(request)}
    )

@app.post("/books/exclude")
def exclude_book(path: str = Form(...), _: None = Depends(require_admin)):
    conn = sqlite3.connect(INVENTORY_DB_PATH)
    conn.execute("UPDATE inventory SET excluded = 1 WHERE path = ?", (path,))
    conn.commit()
    conn.close()
    return Response(status_code=200)

@app.post("/books/mark-extracted")
def mark_book_extracted(request: Request, path: str = Form(...), recipes_extracted: str = Form(""), _: None = Depends(require_admin)):
    """Toggled by the 'Recipes done' checkbox on the library page. An
    unchecked HTML checkbox simply omits its field from the submitted form
    rather than sending a false value, so presence/absence of
    recipes_extracted in the form data (not its content) is what tells us
    the new state."""
    value = 1 if recipes_extracted else 0
    conn = sqlite3.connect(INVENTORY_DB_PATH)
    conn.execute("UPDATE inventory SET recipes_extracted = ? WHERE path = ?", (value, path))
    conn.commit()
    conn.close()
    return templates.TemplateResponse(
        request=request, name="recipes_checkbox.html",
        context={"book": {"path": path, "recipes_extracted": value}}
    )

@app.get("/books/edit-title-author")
def edit_book_title_author_form(request: Request, path: str, _: None = Depends(require_admin)):
    book = get_book_title_author(path)
    if book is None:
        return Response(status_code=404)
    return templates.TemplateResponse(
        request=request, name="book_title_author_edit.html", context={"book": book}
    )

@app.get("/books/title-author-display")
def book_title_author_display(request: Request, path: str, _: None = Depends(require_admin)):
    book = get_book_title_author(path)
    if book is None:
        return Response(status_code=404)
    return templates.TemplateResponse(
        request=request, name="book_title_author_display.html", context={"book": book, "is_admin": True}
    )

@app.post("/books/set-title-author")
def set_book_title_author(request: Request, path: str = Form(...), title: str = Form(""), author: str = Form(""), _: None = Depends(require_admin)):
    conn = sqlite3.connect(INVENTORY_DB_PATH)
    conn.execute(
        "UPDATE inventory SET title = ?, author = ? WHERE path = ?",
        (title.strip() or None, author.strip() or None, path),
    )
    conn.commit()
    conn.close()
    book = get_book_title_author(path)
    if book is None:
        return Response(status_code=404)
    return templates.TemplateResponse(
        request=request, name="book_title_author_display.html", context={"book": book, "is_admin": True}
    )

@app.get("/books/view")
def view_book(path: str):
    conn = sqlite3.connect(INVENTORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            i.path AS original_path,
            CASE WHEN o.ocr_status LIKE 'ocr_success%' OR i.status = 'native_force_reocred'
                 THEN o.output_path ELSE NULL END AS ocr_output_path,
            CASE WHEN c.status = 'verified_ok'
                 THEN c.output_path ELSE NULL END AS compressed_path
        FROM inventory i
        LEFT JOIN ocr_results o ON i.path = o.path
        LEFT JOIN compress_results c ON c.path = COALESCE(o.output_path, i.path)
        WHERE i.path = ?
    """, (path,))
    row = cursor.fetchone()
    conn.close()

    if row is None:
        return Response(status_code=404, content="Book not found in database")

    candidates = [row["compressed_path"], row["ocr_output_path"], row["original_path"]]
    file_to_serve = next((p for p in candidates if p and os.path.exists(p)), None)

    if file_to_serve is None:
        return Response(status_code=404, content="File not found on disk (is the drive mounted?)")

    return FileResponse(file_to_serve)


@app.get("/recipes/new")
def new_recipe_form(request: Request, _: None = Depends(require_admin)):
    return templates.TemplateResponse(request=request, name="new_recipe.html", context={})


@app.post("/recipes")
def add_recipe(
    title: str = Form(...),
    source_book: str = Form(""),
    ingredients: str = Form(""),
    instructions: str = Form(""),
    _: None = Depends(require_admin),
):
    create_recipe(title, source_book, ingredients, instructions)
    return RedirectResponse(url="/", status_code=303)


@app.get("/recipes/{recipe_id}")
def recipe_detail(request: Request, recipe_id: int):
    recipe = get_recipe_by_id(recipe_id)
    admin = is_admin(request)
    if recipe["status"] != "approved" and not admin:
        raise HTTPException(status_code=403, detail="This recipe hasn't been approved yet")
    ingredients = recipe["ingredients"].split("\n")
    instructions = recipe["instructions"].split("\n")
    notes = recipe["notes"].split("\n") if recipe["notes"] else []

    user_email = current_user_email(request)
    reviews = get_reviews_for_recipe(recipe_id)
    my_review = get_user_review(recipe_id, user_email) if user_email else None
    made_it_count = sum(1 for r in reviews if r["made_it"])
    rated = [r["rating"] for r in reviews if r["rating"]]
    avg_rating = round(sum(rated) / len(rated), 1) if rated else None
    qc_results = get_qc_results_for_recipe(recipe_id) if admin else []
    relations = get_recipe_relations(recipe_id)

    return templates.TemplateResponse(
        request=request,
        name="recipe_detail.html",
        context={
            "recipe": recipe, "ingredients": ingredients, "instructions": instructions, "notes": notes, "is_admin": admin,
            "user_email": user_email, "reviews": reviews, "my_review": my_review,
            "made_it_count": made_it_count, "avg_rating": avg_rating, "qc_results": qc_results, "relations": relations,
        }
    )


@app.post("/recipes/{recipe_id}/review")
def submit_review(
    request: Request,
    recipe_id: int,
    made_it: str = Form(""),
    rating: str = Form(""),
    review_text: str = Form(""),
):
    recipe = get_recipe_by_id(recipe_id)
    if recipe is None:
        return Response(status_code=404, content="Recipe not found")
    if recipe["status"] != "approved" and not is_admin(request):
        raise HTTPException(status_code=403, detail="This recipe hasn't been approved yet")

    user_email = current_user_email(request)
    if not user_email:
        # Shouldn't normally happen -- Cloudflare Access authenticates every
        # visitor before they get this far -- but fail loudly rather than
        # silently attributing the review to nobody.
        raise HTTPException(status_code=400, detail="Couldn't identify who you are -- are you accessing this through Cloudflare Access?")

    rating_int = None
    if rating.strip().isdigit():
        candidate = int(rating.strip())
        if 1 <= candidate <= 5:
            rating_int = candidate

    upsert_review(recipe_id, user_email, made_it == "on", rating_int, review_text.strip() or None)
    return RedirectResponse(url=f"/recipes/{recipe_id}", status_code=303)


@app.get("/recipes/{recipe_id}/source")
def recipe_source(request: Request, recipe_id: int):
    recipe = get_recipe_by_id(recipe_id)
    if recipe is None:
        return Response(status_code=404, content="Recipe not found")
    if recipe["status"] != "approved" and not is_admin(request):
        raise HTTPException(status_code=403, detail="This recipe hasn't been approved yet")
    source_path = recipe["source_path"]
    if not source_path or not os.path.exists(source_path):
        return Response(status_code=404, content="Source file not found on disk (is the drive mounted?)")
    return FileResponse(source_path)


@app.get("/recipes/{recipe_id}/edit")
def edit_recipe_form(request: Request, recipe_id: int, _: None = Depends(require_admin)):
    recipe = get_recipe_by_id(recipe_id)
    book_recipes = get_recipes_for_book(recipe["source_book"], exclude_id=recipe_id)
    needs_ids = get_authored_relation_ids(recipe_id, "needs")
    pairs_with_ids = get_authored_relation_ids(recipe_id, "pairs_with")
    return templates.TemplateResponse(
        request=request, name="recipe_edit.html",
        context={
            "recipe": recipe, "book_recipes": book_recipes,
            "needs_ids": needs_ids, "pairs_with_ids": pairs_with_ids,
        }
    )


@app.post("/recipes/{recipe_id}/edit")
async def edit_recipe(
    request: Request,
    recipe_id: int,
    title: str = Form(...),
    servings: str = Form(""),
    prep_time: str = Form(""),
    cook_time: str = Form(""),
    ingredients: str = Form(""),
    instructions: str = Form(""),
    notes: str = Form(""),
    flagged_for_review: str = Form(""),
    _: None = Depends(require_admin),
):
    form = await request.form()
    needs_ids = [int(v) for v in form.getlist("needs_ids") if v]
    pairs_with_ids = [int(v) for v in form.getlist("pairs_with_ids") if v]
    update_recipe(recipe_id, title, servings, prep_time, cook_time, ingredients, instructions, notes, flagged_for_review)
    set_recipe_relations(recipe_id, needs_ids, pairs_with_ids)
    # Re-run QC right after a save, not just on demand via the "Rerun
    # checks" button -- a save is exactly the moment the recipe's content
    # just changed, so the QC table showing stale results (e.g. a "no
    # extraction warning" failure still quoting text you already fixed)
    # until a separate click was surprising. Cheap: these are the same
    # deterministic, no-model-call checks the button runs.
    conn = sqlite3.connect("recipes.db")
    _run_recipe_qc_checks(conn, recipe_id)
    conn.close()
    return RedirectResponse(url=f"/recipes/{recipe_id}", status_code=303)


@app.post("/recipes/{recipe_id}/qc/{check_name}/ack")
def ack_qc_check(request: Request, recipe_id: int, check_name: str, acknowledged: str = Form(""), _: None = Depends(require_admin)):
    """Toggled by the "Reviewed, OK" checkbox next to a failed ADVISORY QC
    check on the recipe page -- lets you confirm a heuristic false
    positive (you've looked at it and it's fine) without having to edit
    the recipe itself. The severity = 'advisory' clause below means this
    can never silently wave away a hard check failure, even from a
    hand-crafted request -- a hard check only ever clears by actually
    fixing the recipe. See run_checks() in pipeline/recipe_qc.py for how
    this flag is preserved or reset the next time QC is re-run."""
    value = 1 if acknowledged else 0
    conn = sqlite3.connect("recipes.db")
    conn.execute(
        "UPDATE recipe_qc_results SET acknowledged = ? WHERE recipe_id = ? AND check_name = ? AND severity = 'advisory'",
        (value, recipe_id, check_name),
    )
    conn.commit()
    conn.close()
    return templates.TemplateResponse(
        request=request, name="qc_ack_checkbox.html",
        context={"recipe": {"id": recipe_id}, "c": {"check_name": check_name, "acknowledged": value}}
    )


@app.post("/recipes/{recipe_id}/qc/rerun")
def rerun_recipe_qc(recipe_id: int, _: None = Depends(require_admin)):
    """The "Rerun checks" button on the recipe page -- the same thing
    `python3 recipe_qc.py --recipe-id <id>` does from the command line,
    exposed here so a manual edit can be re-checked immediately without
    dropping to a shell. run_checks() upserts, so this is safe to click
    as many times as you like."""
    conn = sqlite3.connect("recipes.db")
    _run_recipe_qc_checks(conn, recipe_id)
    conn.close()
    return RedirectResponse(url=f"/recipes/{recipe_id}", status_code=303)


@app.post("/recipes/{recipe_id}/approve")
def approve_recipe(recipe_id: int, _: None = Depends(require_admin)):
    conn = sqlite3.connect("recipes.db")
    conn.execute("UPDATE recipes SET status = 'approved' WHERE id = ?", (recipe_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/review", status_code=303)


@app.post("/recipes/{recipe_id}/unapprove")
def unapprove_recipe(recipe_id: int, _: None = Depends(require_admin)):
    """Puts a live recipe back into the review queue -- for a recipe that
    was approved too hastily and needs another look (a QC issue spotted
    later, or just a change of mind). Lands back on the recipe's own page
    rather than /review, since unapproving is usually the first step
    toward fixing something on this exact recipe, not a queue-clearing
    action."""
    conn = sqlite3.connect("recipes.db")
    conn.execute("UPDATE recipes SET status = 'pending' WHERE id = ?", (recipe_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/recipes/{recipe_id}", status_code=303)


@app.delete("/recipes/{recipe_id}")
def delete_recipe(recipe_id: int, _: None = Depends(require_admin)):
    conn = sqlite3.connect("recipes.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM recipe_reviews WHERE recipe_id = ?", (recipe_id,))
    cursor.execute("DELETE FROM recipe_qc_results WHERE recipe_id = ?", (recipe_id,))
    cursor.execute("DELETE FROM recipe_relations WHERE recipe_id = ? OR related_recipe_id = ?", (recipe_id, recipe_id))
    cursor.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    conn.commit()
    conn.close()
    return Response(status_code=200)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
