#!/usr/bin/env python3
"""
Removes a book's recipes from recipes.db -- along with every row in
recipe_reviews, recipe_qc_results, and recipe_relations that references
them -- and optionally clears its extraction_log history too, so a fresh
extraction run starts completely clean.

Why this exists: reprocessing a book with a different --pages-per-chunk
(e.g. redoing a 15-page-chunk extraction at 8 pages to catch recipes the
larger chunks lost -- see RUNBOOK.md -> "Reprocessing a book with
different chunk settings" for why smaller chunks catch more) either needs
the old rows cleared first (a clean replace), or needs the losing half of
a side-by-side comparison cleared afterward once winners are picked. In
both cases, deleting only from `recipes` and leaving the other three
tables behind recreates exactly the "ghost row" problem this project
already has a name for -- extract_recipes.py's insert_recipe() has no
dedup check, so a naive re-run just adds more rows on top of the old ones
rather than replacing them.

This generalizes the same cleanup main.py's delete_recipe() route does for
a single recipe (recipe_relations, recipe_reviews, recipe_qc_results, then
the recipe row itself -- see the 2026-09-28 "delete_recipe fix that
wasn't" entry in PROJECT_HISTORY.md for why that route only *just* started
doing this completely) to a whole book at once, and adds the one thing a
single-recipe delete never needs to worry about: extraction_log, keyed on
source_path/chunk_index/engine with no link to a specific recipe at all.
Clearing it is optional and separate from --apply, because it's only
useful before a clean re-extraction -- there's no reason to touch it when
you're just removing a handful of losing duplicates after a compare.

SAFE BY DEFAULT: running this with no flags only *reports* what it would
delete. Nothing is removed from the database until you pass --apply.
extraction_log rows are left alone even with --apply, unless you also
pass --clear-extraction-log.

Usage:
    # 1. Always look first -- prints exactly what would be removed.
    python3 remove_book_recipes.py --book "Nigella Lawson - Feast"

    # 2. Full clean replace: delete every recipe for this book (any
    #    status) plus its extraction_log history, so a re-run of
    #    extract_recipes.py starts from a totally blank slate.
    python3 remove_book_recipes.py --book "Nigella Lawson - Feast" \\
        --apply --clear-extraction-log

    # 3. Compare-side-by-side cleanup: after reviewing the new pending
    #    recipes from a re-run at a smaller chunk size against the
    #    existing approved ones and picking winners, remove just the
    #    losing *pending* duplicates -- leaves the approved originals and
    #    extraction_log (there's nothing to resume, so no reason to
    #    clear it) untouched.
    python3 remove_book_recipes.py --book "Nigella Lawson - Feast" \\
        --status pending --apply

    # 4. Trim a bad partial run: if a re-extraction went wrong partway
    #    through (e.g. wrong PDF page range, a crashed run) and you can
    #    see in the review queue exactly which id the good recipes start
    #    at, remove everything below that id for this book and leave the
    #    rest alone. Can't be combined with --clear-extraction-log --
    #    extraction_log has no recipe id in it, so there'd be no way to
    #    clear only the log rows for the deleted range.
    python3 remove_book_recipes.py --book "Nigella Lawson - Feast" \\
        --before-id 2145 --apply
"""

import argparse
import os
import sqlite3


def find_matching_recipes(cursor, book, status=None, before_id=None):
    """Every `recipes` row for this book (exact source_book match, same
    convention as recipe_qc.py's --book), optionally narrowed to one
    status and/or to ids below before_id. Returns list of dicts with
    id/title/status/source_path."""
    query = "SELECT id, title, status, source_path FROM recipes WHERE source_book = ?"
    params = [book]
    if status:
        query += " AND status = ?"
        params.append(status)
    if before_id is not None:
        query += " AND id < ?"
        params.append(before_id)
    cursor.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]


def count_where_in(cursor, table, column, values):
    if not values:
        return 0
    placeholders = ",".join("?" * len(values))
    cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({placeholders})", values)
    return cursor.fetchone()[0]


def count_relations(cursor, ids):
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    cursor.execute(
        f"SELECT COUNT(*) FROM recipe_relations "
        f"WHERE recipe_id IN ({placeholders}) OR related_recipe_id IN ({placeholders})",
        ids + ids,
    )
    return cursor.fetchone()[0]


def extraction_log_counts(cursor, source_paths):
    """How many extraction_log rows exist for each distinct source_path
    these recipes came from -- shown regardless of --clear-extraction-log,
    since it's useful context even when you're not clearing them."""
    counts = {}
    for path in source_paths:
        cursor.execute("SELECT COUNT(*) FROM extraction_log WHERE source_path = ?", (path,))
        counts[path] = cursor.fetchone()[0]
    return counts


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--book", required=True, help="source_book to match EXACTLY (case and punctuation included)")
    parser.add_argument("--db", default=os.path.expanduser("~/recipe-app/recipes.db"))
    parser.add_argument(
        "--status", choices=["pending", "approved"], default=None,
        help="Only match recipes with this status (default: every status)",
    )
    parser.add_argument(
        "--before-id", type=int, default=None, metavar="ID",
        help="Only match recipes with id < ID -- useful for trimming a bad "
             "partial extraction run once you know the first id that looks "
             "right (default: no id filter). Can't be combined with "
             "--clear-extraction-log.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually delete the matching rows (default: dry run, report only)",
    )
    parser.add_argument(
        "--clear-extraction-log", action="store_true",
        help="Also delete extraction_log rows for this book's source path(s) -- "
             "requires --apply. Only do this before a full clean re-extraction; "
             "it's not needed just to remove some duplicate recipes.",
    )
    args = parser.parse_args()

    if args.clear_extraction_log and not args.apply:
        parser.error("--clear-extraction-log requires --apply")

    if args.before_id is not None and args.clear_extraction_log:
        parser.error(
            "--before-id can't be combined with --clear-extraction-log: "
            "extraction_log rows aren't linked to a recipe id (only to "
            "source_path), so clearing the log here would wipe this "
            "book's WHOLE extraction history -- including the chunks "
            "behind the recipes you're keeping -- not just the range "
            "you're deleting. Re-run without --clear-extraction-log."
        )

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    matches = find_matching_recipes(cursor, args.book, status=args.status, before_id=args.before_id)

    if not matches:
        status_bit = f" with status={args.status}" if args.status else ""
        if args.before_id is not None:
            status_bit += f" with id < {args.before_id}"
        print(f"No recipes found for source_book={args.book!r}{status_bit}")
        conn.close()
        return

    ids = [m["id"] for m in matches]
    source_paths = sorted({m["source_path"] for m in matches if m["source_path"]})

    review_count = count_where_in(cursor, "recipe_reviews", "recipe_id", ids)
    qc_count = count_where_in(cursor, "recipe_qc_results", "recipe_id", ids)
    relation_count = count_relations(cursor, ids)
    log_counts = extraction_log_counts(cursor, source_paths)
    total_log_rows = sum(log_counts.values())

    status_bit = f" (status={args.status})" if args.status else " (any status)"
    if args.before_id is not None:
        status_bit += f", id < {args.before_id}"
    print(f"{len(matches)} recipe(s) match source_book={args.book!r}{status_bit}:\n")
    for m in matches:
        print(f"  [{m['status']:>8}] #{m['id']:<5} {m['title']}")
    print()
    print(f"Would also remove:")
    print(f"  -> {review_count} recipe_reviews row(s)")
    print(f"  -> {qc_count} recipe_qc_results row(s)")
    print(f"  -> {relation_count} recipe_relations row(s) (either direction)")
    print()
    if source_paths:
        print("extraction_log rows found for this book's source path(s):")
        for path in source_paths:
            print(f"  {log_counts[path]:>4} row(s) -- {path}")
        if args.clear_extraction_log:
            print(f"  -> {total_log_rows} extraction_log row(s) WILL be cleared (--clear-extraction-log passed)")
        else:
            print(f"  -> left alone (pass --clear-extraction-log to also clear these, "
                  f"e.g. before a full re-extraction)")
        print()

    if not args.apply:
        print("DRY RUN -- nothing deleted. Re-run with --apply to actually delete these rows.")
        conn.close()
        return

    def delete_where_in(table, column, values):
        if not values:
            return 0
        placeholders = ",".join("?" * len(values))
        cursor.execute(f"DELETE FROM {table} WHERE {column} IN ({placeholders})", values)
        return cursor.rowcount

    reviews_deleted = delete_where_in("recipe_reviews", "recipe_id", ids)
    qc_deleted = delete_where_in("recipe_qc_results", "recipe_id", ids)
    placeholders = ",".join("?" * len(ids))
    cursor.execute(
        f"DELETE FROM recipe_relations "
        f"WHERE recipe_id IN ({placeholders}) OR related_recipe_id IN ({placeholders})",
        ids + ids,
    )
    relations_deleted = cursor.rowcount
    recipes_deleted = delete_where_in("recipes", "id", ids)

    log_deleted = 0
    if args.clear_extraction_log:
        for path in source_paths:
            cursor.execute("DELETE FROM extraction_log WHERE source_path = ?", (path,))
            log_deleted += cursor.rowcount

    conn.commit()

    print(f"Deleted {recipes_deleted} recipe(s), {reviews_deleted} recipe_reviews row(s), "
          f"{qc_deleted} recipe_qc_results row(s), {relations_deleted} recipe_relations row(s).")
    if args.clear_extraction_log:
        print(f"Also cleared {log_deleted} extraction_log row(s) -- this book will "
              f"re-extract from scratch on the next run (no --resume skip).")

    conn.close()


if __name__ == "__main__":
    main()
