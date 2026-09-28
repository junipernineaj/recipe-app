#!/usr/bin/env python3
"""
Renames a book's source_book value across every recipe that has it --
for a book that's internally consistent (every recipe agrees) but wrong,
most commonly stored "Title - Author" instead of the documented
"Author - Title" convention. That's a different problem from the one
find_inconsistent_book_titles.py catches: that script finds a book split
across two source_book values; this one fixes a single, consistently-wrong
value once a human (who actually knows the book) has spotted it -- there's
no reliable way to tell "Title - Author" from "Author - Title" purely from
the text, so this is deliberately a manual, one-book-at-a-time tool rather
than an auto-detector.

SAFE BY DEFAULT: running this with no flags only *reports* what it would
rename. Nothing changes until you pass --apply. If --new-name already has
recipes of its own, this becomes a merge (both groups end up sharing one
source_book) -- the report says so up front, so that's never a surprise.

Usage:
    # 1. Always look first -- prints exactly what would be renamed.
    python3 rename_book_source_book.py \\
        --old-name "Japaneasy - Tim Anderson" --new-name "Tim Anderson - Japaneasy"

    # 2. Actually rename.
    python3 rename_book_source_book.py \\
        --old-name "Japaneasy - Tim Anderson" --new-name "Tim Anderson - Japaneasy" --apply
"""

import argparse
import os
import sqlite3


def find_matching_recipes(cursor, source_book):
    cursor.execute(
        "SELECT id, title, status FROM recipes WHERE source_book = ? ORDER BY id", (source_book,)
    )
    return [dict(row) for row in cursor.fetchall()]


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--old-name", required=True, help="current source_book value, matched EXACTLY")
    parser.add_argument("--new-name", required=True, help="source_book value to rename it to")
    parser.add_argument("--db", default=os.path.expanduser("~/recipe-app/recipes.db"))
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually rename the matching rows (default: dry run, report only)",
    )
    args = parser.parse_args()

    if args.old_name == args.new_name:
        parser.error("--old-name and --new-name are identical -- nothing to rename")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    matches = find_matching_recipes(cursor, args.old_name)
    if not matches:
        print(f"No recipes found for source_book={args.old_name!r} -- nothing to do.")
        conn.close()
        return

    existing_new = find_matching_recipes(cursor, args.new_name)

    status_counts = {}
    for m in matches:
        status_counts[m["status"]] = status_counts.get(m["status"], 0) + 1
    status_summary = ", ".join(f"{count} {status}" for status, count in sorted(status_counts.items()))

    print(f"{len(matches)} recipe(s) match source_book={args.old_name!r} ({status_summary}):\n")
    for m in matches:
        print(f"  [{m['status']:>8}] #{m['id']:<5} {m['title']}")
    print()

    if existing_new:
        print(f"NOTE: {len(existing_new)} recipe(s) already use source_book={args.new_name!r} -- "
              f"this rename will MERGE the two groups into one book, not just relabel an empty name.")
        print()

    if not args.apply:
        print("DRY RUN -- nothing renamed. Re-run with --apply to actually rename these rows.")
        conn.close()
        return

    cursor.execute(
        "UPDATE recipes SET source_book = ? WHERE source_book = ?", (args.new_name, args.old_name)
    )
    renamed = cursor.rowcount
    conn.commit()
    conn.close()

    print(f"Renamed {renamed} recipe(s) from source_book={args.old_name!r} to {args.new_name!r}.")


if __name__ == "__main__":
    main()
