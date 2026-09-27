#!/usr/bin/env python3
"""
Tier-1 recipe QC: cheap, deterministic, no-model-call sanity checks run
against every recipe already sitting in recipes.db.

This is the first half of a two-phase quality system. Tier 1 (this file)
catches the mechanical stuff instantly and for free -- too few ingredients,
a stray '%' where a garbled fraction (1/2, 1/4, 1/8) probably belongs,
suspiciously short parsing-artifact lines, an ingredient that's never
mentioned anywhere in the method, and the extraction model's own "this
looked off" flag. Tier 2 (not built yet) will be a real LLM call
comparing a recipe against its original source-page text for a proper
semantic double-check -- deliberately kept separate and on-demand, since
that one costs real tokens per recipe and this one doesn't.

Each check is a plain function (ingredients: list[str], instructions:
list[str], flagged_for_review: str | None) -> (passed: bool, detail: str
| None). Most checks ignore flagged_for_review -- it's only there so a
check can key off it (see check_no_extraction_warning below). Adding a
new rule later is just adding one more function and a line in CHECKS --
nothing else about the schema, the pipeline hook, or the UI needs to
change.

Results land in a new recipe_qc_results table (one row per recipe per
check). check_no_extraction_warning below is what surfaces the existing
flagged_for_review column as one of these checks, so it shows up
alongside everything else in the same pass/fail table instead of only
living in its own spot on the page -- but the underlying column is still
the *model's own* "something looked off" signal from extraction time,
kept as-is rather than folded into or duplicated by the other checks.

Usage:
    # Backfill every recipe already in the database:
    python3 recipe_qc.py --db ~/recipe-app/recipes.db

    # Just one book:
    python3 recipe_qc.py --db ~/recipe-app/recipes.db --book "Nigella Lawson - Feast"

    # Just one recipe (e.g. after a manual edit):
    python3 recipe_qc.py --db ~/recipe-app/recipes.db --recipe-id 42

extract_recipes.py and extract_recipes_local.py also call run_checks()
directly on every recipe right after it's inserted, so newly-extracted
books get checked automatically -- this script is for backfilling
whatever was extracted before this existed, and for re-running after a
rule changes.
"""
import argparse
import re
import sqlite3


def init_qc_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recipe_qc_results (
            recipe_id INTEGER NOT NULL,
            check_name TEXT NOT NULL,
            passed INTEGER NOT NULL,
            severity TEXT NOT NULL,
            detail TEXT,
            checked_at TEXT NOT NULL,
            PRIMARY KEY (recipe_id, check_name)
        )
    """)
    conn.commit()


def check_min_ingredients(ingredients, instructions, flagged_for_review):
    ok = len(ingredients) > 2
    if ok:
        return True, None
    return False, f"Only {len(ingredients)} ingredient line(s) -- expected more than 2."


def check_min_instructions(ingredients, instructions, flagged_for_review):
    ok = len(instructions) >= 1
    if ok:
        return True, None
    return False, "No instruction steps at all."


def check_no_percent_artifacts(ingredients, instructions, flagged_for_review):
    # A genuine '%' in an ingredient line is vanishingly rare in a home
    # cookbook -- in practice this has turned out to be a reliable tell
    # for a fraction glyph (½, ¼, ⅛) that got mangled somewhere between
    # OCR and JSON, e.g. "1% cups flour" where "1½" was intended.
    bad = [line for line in ingredients if "%" in line]
    if not bad:
        return True, None
    return False, "Percent sign found in: " + "; ".join(bad) + " -- likely a mis-OCR'd fraction (1/2, 1/4, 1/8)."


def check_no_short_lines(ingredients, instructions, flagged_for_review):
    short_ing = [line for line in ingredients if len(line.strip()) <= 2]
    short_ins = [line for line in instructions if len(line.strip()) <= 2]
    if not short_ing and not short_ins:
        return True, None
    parts = []
    if short_ing:
        parts.append(f"ingredient line(s) {short_ing!r}")
    if short_ins:
        parts.append(f"instruction line(s) {short_ins!r}")
    return False, "Suspiciously short " + " and ".join(parts) + " -- likely a parsing artifact rather than real content."


# Words stripped out before comparing an ingredient line against the method
# text -- quantities, units, and prep/filler words that would never be
# repeated verbatim in a method step even for a perfectly good recipe.
_FILLER_WORDS = {
    "the", "and", "or", "to", "for", "of", "a", "an", "with", "plus", "extra",
    "taste", "serving", "servings", "garnish", "garnishing", "optional",
    "large", "small", "medium", "fresh", "dried", "ground", "finely",
    "roughly", "coarsely", "chopped", "sliced", "diced", "crushed", "grated",
    "peeled", "trimmed", "halved", "quartered", "room", "temperature",
    "about", "approximately", "each", "into", "pieces", "piece", "handful",
    "good", "quality", "plain", "self", "raising", "unsalted", "salted",
    "level", "heaped", "tablespoons", "tablespoon", "teaspoons", "teaspoon",
}
_WORD_RE = re.compile(r"[a-zA-Z]+")


def _significant_words(text: str) -> set:
    """Crude noun-ish keyword extraction -- not real NLP, just enough to
    catch the common case where an ingredient is never mentioned again
    anywhere in the method. Words under 4 letters are dropped too (cuts
    out most remaining units and short filler that slipped past the
    stopword list)."""
    words = _WORD_RE.findall(text.lower())
    return {w for w in words if len(w) >= 4 and w not in _FILLER_WORDS}


def check_ingredients_used_in_method(ingredients, instructions, flagged_for_review):
    # Deliberately advisory, not a hard fail (see CHECKS below): real
    # recipes routinely list an ingredient that's only implied later --
    # "salt and pepper to taste" rarely gets repeated verbatim, garnishes
    # get lumped as "to serve", "2 tbsp oil, plus extra for frying" won't
    # lexically match "heat the oil". A simple word-overlap heuristic will
    # always have false positives for cases like these, so treat a miss
    # here as "worth a glance", not "something is definitely wrong".
    method_words = _significant_words(" ".join(instructions))
    unused = [
        line for line in ingredients
        if _significant_words(line) and not (_significant_words(line) & method_words)
    ]
    if not unused:
        return True, None
    return False, (
        "Possibly unused in method: " + "; ".join(unused)
        + " (heuristic word match -- false positives are common, e.g. 'salt and pepper to taste', garnishes)."
    )


def check_no_extraction_warning(ingredients, instructions, flagged_for_review):
    # Not a new signal -- this just surfaces the flagged_for_review column
    # (set by the extraction model itself, at extraction time, whenever it
    # thought something looked off about the page) as one row in the same
    # QC table, so a model-flagged recipe and a deterministic-check
    # failure show up in the same place instead of two different ones.
    if not flagged_for_review:
        return True, None
    return False, flagged_for_review


# (check_name, severity, function). severity is "hard" (a real problem --
# these are what should drive attention/priority) or "advisory" (worth a
# glance, but expect some false positives). Add a new rule by adding a
# function above and a line here -- nothing else needs to change.
CHECKS = [
    ("min_ingredients", "hard", check_min_ingredients),
    ("min_instructions", "hard", check_min_instructions),
    ("no_percent_artifacts", "hard", check_no_percent_artifacts),
    ("no_short_lines", "hard", check_no_short_lines),
    ("no_extraction_warning", "hard", check_no_extraction_warning),
    ("ingredients_used_in_method", "advisory", check_ingredients_used_in_method),
]


def run_checks(conn, recipe_id: int):
    """Runs every registered check against one recipe and upserts the
    results. Safe to call repeatedly -- after a manual edit, after adding
    a new rule, whatever -- each call just overwrites that recipe's rows."""
    cur = conn.execute(
        "SELECT ingredients, instructions, flagged_for_review FROM recipes WHERE id = ?",
        (recipe_id,),
    )
    row = cur.fetchone()
    if row is None:
        return []
    ingredients = [line for line in (row[0] or "").split("\n") if line.strip()]
    instructions = [line for line in (row[1] or "").split("\n") if line.strip()]
    flagged_for_review = row[2]

    results = []
    for name, severity, fn in CHECKS:
        passed, detail = fn(ingredients, instructions, flagged_for_review)
        conn.execute("""
            INSERT INTO recipe_qc_results (recipe_id, check_name, passed, severity, detail, checked_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(recipe_id, check_name) DO UPDATE SET
                passed = excluded.passed, severity = excluded.severity,
                detail = excluded.detail, checked_at = excluded.checked_at
        """, (recipe_id, name, int(passed), severity, detail))
        results.append((name, severity, passed, detail))
    conn.commit()
    return results


def main():
    ap = argparse.ArgumentParser(description="Run tier-1 deterministic QC checks against recipes already in recipes.db")
    ap.add_argument("--db", required=True)
    ap.add_argument("--book", help="only check recipes from this source_book (exact match)")
    ap.add_argument("--recipe-id", type=int, help="only check this one recipe")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    init_qc_table(conn)

    if args.recipe_id:
        ids = [args.recipe_id]
    else:
        query = "SELECT id FROM recipes"
        params = []
        if args.book:
            query += " WHERE source_book = ?"
            params.append(args.book)
        ids = [row[0] for row in conn.execute(query, params).fetchall()]

    print(f"Running {len(CHECKS)} check(s) against {len(ids)} recipe(s)...")
    fail_counts = {name: 0 for name, _, _ in CHECKS}
    for recipe_id in ids:
        for name, severity, passed, detail in run_checks(conn, recipe_id):
            if not passed:
                fail_counts[name] += 1

    print("\n=== QC summary ===")
    for name, severity, _ in CHECKS:
        print(f"  {name} ({severity}): {fail_counts[name]} failed / {len(ids)}")
    conn.close()


if __name__ == "__main__":
    main()
