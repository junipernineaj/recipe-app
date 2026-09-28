#!/usr/bin/env python3
"""
Finds books whose recipes disagree with each other about what source_book
should be -- the same underlying PDF, extracted (or re-extracted) under two
or more different --book-title strings, so it shows up as two separate
"books" everywhere in the app that groups or filters by source_book (the
home page's book filter, the review queue, /qc-issues, and so on). The
usual cause: re-running extraction against the same PDF later on and typing
the book title differently the second time -- an early, unparsed "Title -
Author" (sometimes just the raw filename) the first time around, the
documented "Author - Title" convention afterwards, or a plain typo.

How books are matched up: this resolves each recipe's source_path back to
its inventory.sqlite row the same way main.py's /authors page does --
following the original path, its OCR output path, and its compressed
output path as equivalent names for the same book (extract_recipes.py /
extract_recipes_local.py's --pdf accepts any of the three). Recipes whose
source_path doesn't match any inventory row at all (a book added by hand,
or the drive unmounted when this runs) are grouped by their raw source_path
instead, which still catches an exact re-run at the same path but can't
catch one that used a different path for the same book -- that case would
need the inventory row to exist and be resolvable.

This is read-only. It only reports groups where more than one source_book
value exists for what looks like the same book -- it never changes
anything in recipes.db or inventory.sqlite. Deciding which value is the
"correct" one, and fixing it, is left to you (or a follow-up script, once
you've seen the shape of the report).

Usage:
    python3 find_inconsistent_book_titles.py
    python3 find_inconsistent_book_titles.py --db ~/recipe-app/recipes.db --inventory-db ~/cookbook-project/inventory.sqlite
    python3 find_inconsistent_book_titles.py --csv inconsistent_book_titles_report.csv
"""

import argparse
import csv as csv_module
import os
import sqlite3
from collections import defaultdict


def build_inventory_path_groups(inventory_db_path):
    """Returns (path_to_group_key, group_key_to_info). group_key_to_info
    maps each inventory row's own path (used as the stable group key) to
    its {"title": ..., "author": ...}. path_to_group_key maps every path
    variant (original / OCR output / compressed output) for that row back
    to the same group key -- the same three-way join main.py's
    _build_inventory_title_author_lookup() uses for /authors."""
    conn = sqlite3.connect(inventory_db_path)
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
    path_to_group_key = {}
    group_key_to_info = {}
    for row in cursor.fetchall():
        group_key = row["path"]
        group_key_to_info[group_key] = {"title": row["title"], "author": row["author"]}
        for path in (row["path"], row["ocr_output_path"], row["compressed_output_path"]):
            if path:
                path_to_group_key[path] = group_key
    conn.close()
    return path_to_group_key, group_key_to_info


def find_inconsistencies(recipes_db_path, inventory_db_path):
    """Returns a list of group dicts, most-affected-recipes first. Each
    group has: group_key, inventory_title, inventory_author, and variants
    (a list of {"source_book", "count", "statuses", "min_id", "max_id",
    "extracted_at_min", "extracted_at_max"} sorted by count descending).
    Pure logic, no printing -- kept separate so it can be unit-tested
    without capturing stdout."""
    path_to_group_key, group_key_to_info = build_inventory_path_groups(inventory_db_path)

    conn = sqlite3.connect(recipes_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id, source_book, source_path, status, extracted_at FROM recipes")
    recipes = cursor.fetchall()
    conn.close()

    # group_key -> source_book -> list of recipe rows
    grouped = defaultdict(lambda: defaultdict(list))
    for r in recipes:
        group_key = path_to_group_key.get(r["source_path"], r["source_path"])
        grouped[group_key][r["source_book"]].append(r)

    groups = []
    for group_key, by_title in grouped.items():
        if len(by_title) < 2:
            continue
        variants = []
        for source_book, rows in by_title.items():
            statuses = sorted({row["status"] for row in rows})
            extracted_ats = sorted(row["extracted_at"] for row in rows if row["extracted_at"])
            variants.append({
                "source_book": source_book,
                "count": len(rows),
                "statuses": statuses,
                "min_id": min(row["id"] for row in rows),
                "max_id": max(row["id"] for row in rows),
                "extracted_at_min": extracted_ats[0] if extracted_ats else None,
                "extracted_at_max": extracted_ats[-1] if extracted_ats else None,
            })
        variants.sort(key=lambda v: -v["count"])
        info = group_key_to_info.get(group_key, {})
        groups.append({
            "group_key": group_key,
            "inventory_title": info.get("title"),
            "inventory_author": info.get("author"),
            "variants": variants,
        })

    groups.sort(key=lambda g: -sum(v["count"] for v in g["variants"]))
    return groups


def print_report(groups):
    if not groups:
        print("No inconsistent book titles found -- every book's recipes agree on one source_book.")
        return

    total_recipes = sum(v["count"] for g in groups for v in g["variants"])
    print(f"Found {len(groups)} book(s) with more than one source_book value "
          f"({total_recipes} recipes involved):\n")

    for i, g in enumerate(groups, 1):
        label_bits = []
        if g["inventory_author"] or g["inventory_title"]:
            label_bits.append(f"{g['inventory_author'] or 'Unknown'} - {g['inventory_title'] or '(no title set)'}")
        label = f" ({label_bits[0]})" if label_bits else ""
        print(f"--- Group {i}: {g['group_key']}{label} ---")
        for v in g["variants"]:
            date_range = ""
            if v["extracted_at_min"]:
                if v["extracted_at_min"] == v["extracted_at_max"]:
                    date_range = f", extracted {v['extracted_at_min']}"
                else:
                    date_range = f", extracted {v['extracted_at_min']} to {v['extracted_at_max']}"
            print(f"  {v['count']:>4} recipe(s)  source_book={v['source_book']!r}  "
                  f"status={'/'.join(v['statuses'])}  ids {v['min_id']}-{v['max_id']}{date_range}")
        print()

    print("Nothing above has been changed -- this is a read-only report. Once you've decided "
          "which source_book each group should use, a follow-up script can normalize them.")


def write_csv(path, groups):
    with open(path, "w", newline="") as f:
        writer = csv_module.writer(f)
        writer.writerow([
            "group", "group_key", "inventory_title", "inventory_author",
            "source_book", "recipe_count", "statuses", "min_id", "max_id",
            "extracted_at_min", "extracted_at_max",
        ])
        for i, g in enumerate(groups, 1):
            for v in g["variants"]:
                writer.writerow([
                    i, g["group_key"], g["inventory_title"] or "", g["inventory_author"] or "",
                    v["source_book"], v["count"], "/".join(v["statuses"]), v["min_id"], v["max_id"],
                    v["extracted_at_min"] or "", v["extracted_at_max"] or "",
                ])
    print(f"\nWrote CSV report to {path}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", default=os.path.expanduser("~/recipe-app/recipes.db"),
                         help="recipes.db to scan")
    parser.add_argument("--inventory-db", default=os.path.expanduser("~/cookbook-project/inventory.sqlite"),
                         help="inventory.sqlite, used to resolve a recipe's source_path back to one book "
                              "across its original/OCR/compressed path variants")
    parser.add_argument("--csv", default=None, help="Also write a CSV report to this path")
    args = parser.parse_args()

    print(f"Scanning {args.db} (resolved against {args.inventory_db})...")
    groups = find_inconsistencies(args.db, args.inventory_db)
    print_report(groups)

    if args.csv:
        write_csv(args.csv, groups)


if __name__ == "__main__":
    main()
