#!/usr/bin/env python3
"""
Finds inventory entries that are actual duplicate books -- the same
underlying file content registered under two (or more) different paths in
`inventory` -- rather than just two different books whose names happen to
look alike.

Why hash the *resolved* file rather than the raw original: two inventory
rows can be genuinely the same book even though their original paths never
matched (the same file re-added to the holding pen under a new name or
location, a Calibre re-export, etc). This script resolves each row to the
same "best available" copy /books/view would actually serve -- compressed
(only if verified_ok) -> OCR'd (only if it succeeded) -> the raw original
-- and hashes *that*. Two rows for the same book converge on identical
bytes at that point even when nothing about their original filenames
lined up.

This is read-only. It only reports groups of paths whose resolved files
are byte-identical -- it never deletes or changes anything in
inventory.sqlite or on disk. What to do with a group is a judgment call
(the existing "Not a cookbook" exclude button on /books, a manual delete
of the extra file, or reprocessing) that this script leaves to you.

Note: this finds *exact* duplicates only (identical bytes). Two scans of
the same book from different sources, or a book you fixed a typo in and
re-OCR'd, won't hash the same and won't show up here -- that's deliberate,
to keep the false-positive rate at zero. If you're expecting a duplicate
that isn't listed, it may be a near-duplicate rather than an exact one.

Usage:
    python3 find_duplicate_books.py
    python3 find_duplicate_books.py --db ~/cookbook-project/inventory.sqlite
    python3 find_duplicate_books.py --csv duplicate_books_report.csv
    python3 find_duplicate_books.py --include-excluded
"""

import argparse
import csv as csv_module
import hashlib
import os
import sqlite3
from collections import defaultdict


def table_columns(cursor, table_name):
    cursor.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor.fetchall()}


def resolve_file_for_path(cursor, path):
    """Same fallback chain as /books/view in main.py: compressed (only if
    verified_ok) -> OCR'd (only if it succeeded) -> the raw original.
    Returns (resolved_path, tier), or (None, None) if nothing for this
    book exists on disk right now (e.g. the drive isn't mounted)."""
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
    if row is None:
        return None, None
    candidates = [
        (row["compressed_path"], "compressed"),
        (row["ocr_output_path"], "ocr"),
        (row["original_path"], "original"),
    ]
    for candidate_path, tier in candidates:
        if candidate_path and os.path.exists(candidate_path):
            return candidate_path, tier
    return None, None


def hash_file(path, chunk_size=1024 * 1024):
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def find_duplicates(db_path, include_excluded=False):
    """Returns (duplicate_groups, missing_on_disk, total_scanned). Each
    group is a list of dicts with inventory_path/filename/title/author/
    file_path/tier/size/hash. Pure logic, no printing -- kept separate so
    it can be unit-tested without capturing stdout."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    inventory_columns = table_columns(cursor, "inventory")
    select_columns = ["path", "filename"]
    for optional in ("title", "author", "excluded"):
        if optional in inventory_columns:
            select_columns.append(optional)

    query = f"SELECT {', '.join(select_columns)} FROM inventory"
    if not include_excluded and "excluded" in inventory_columns:
        query += " WHERE excluded = 0"
    cursor.execute(query)
    books = cursor.fetchall()

    resolved = []
    missing_on_disk = []
    for book in books:
        file_path, tier = resolve_file_for_path(cursor, book["path"])
        if file_path is None:
            missing_on_disk.append(book["path"])
            continue
        resolved.append({
            "inventory_path": book["path"],
            "filename": book["filename"],
            "title": book["title"] if "title" in book.keys() else None,
            "author": book["author"] if "author" in book.keys() else None,
            "file_path": file_path,
            "tier": tier,
            "size": os.path.getsize(file_path),
        })
    conn.close()

    # Pass 1: group by exact file size. Two unrelated files essentially
    # never collide on byte size, so this cheaply rules out the vast
    # majority of books before any hashing happens.
    by_size = defaultdict(list)
    for entry in resolved:
        by_size[entry["size"]].append(entry)
    size_candidates = [group for group in by_size.values() if len(group) > 1]

    # Pass 2: hash only the same-size candidates, then group by hash.
    by_hash = defaultdict(list)
    for group in size_candidates:
        for entry in group:
            entry["hash"] = hash_file(entry["file_path"])
            by_hash[entry["hash"]].append(entry)

    duplicate_groups = [group for group in by_hash.values() if len(group) > 1]
    duplicate_groups.sort(key=lambda g: -len(g))

    return duplicate_groups, missing_on_disk, len(resolved)


def print_report(duplicate_groups, missing_on_disk, total_scanned):
    print(f"{total_scanned} book(s) resolved to a file on disk "
          f"({len(missing_on_disk)} not found -- drive unmounted, or moved).")
    print()

    if not duplicate_groups:
        print("No exact duplicates found.")
    else:
        total_extra_copies = sum(len(g) - 1 for g in duplicate_groups)
        copies_word = "copy" if total_extra_copies == 1 else "copies"
        print(f"Found {len(duplicate_groups)} group(s) of true duplicates "
              f"({total_extra_copies} redundant {copies_word}):\n")
        for i, group in enumerate(duplicate_groups, 1):
            size_mb = group[0]["size"] / 1048576
            print(f"--- Group {i}: {len(group)} identical copies, {size_mb:.1f} MB each "
                  f"(sha256 {group[0]['hash'][:12]}...) ---")
            for entry in group:
                label = entry["title"] or entry["filename"]
                author_bit = f" by {entry['author']}" if entry["author"] else ""
                print(f"  [{entry['tier']:>10}] {label}{author_bit}")
                print(f"               inventory path: {entry['inventory_path']}")
            print()

    if missing_on_disk:
        print(f"({len(missing_on_disk)} book(s) skipped -- no file found on disk for "
              f"the original, OCR'd, or compressed copy. Run with the drive mounted "
              f"to include them.)")


def write_csv(path, duplicate_groups):
    with open(path, "w", newline="") as f:
        writer = csv_module.writer(f)
        writer.writerow(["group", "inventory_path", "resolved_file", "tier", "size_bytes", "title", "author"])
        for i, group in enumerate(duplicate_groups, 1):
            for entry in group:
                writer.writerow([
                    i, entry["inventory_path"], entry["file_path"], entry["tier"],
                    entry["size"], entry["title"] or "", entry["author"] or "",
                ])
    print(f"\nWrote CSV report to {path}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", default=os.path.expanduser("~/cookbook-project/inventory.sqlite"))
    parser.add_argument("--csv", default=None, help="Also write a CSV report to this path")
    parser.add_argument(
        "--include-excluded", action="store_true",
        help="Also check books already flagged 'Not a cookbook' (skipped by default)",
    )
    args = parser.parse_args()

    print(f"Scanning {args.db}...")
    duplicate_groups, missing_on_disk, total_scanned = find_duplicates(
        args.db, include_excluded=args.include_excluded
    )
    print_report(duplicate_groups, missing_on_disk, total_scanned)

    if args.csv:
        write_csv(args.csv, duplicate_groups)


if __name__ == "__main__":
    main()
