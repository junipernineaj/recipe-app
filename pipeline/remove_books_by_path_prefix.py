#!/usr/bin/env python3
"""
Removes every inventory entry -- and the ocr_results / compress_results
rows that came with it -- whose path falls under a given folder prefix.

Written for the 2026-09 "Keeping" incident: an accidentally-created
subfolder under the Cookbooks source directory
(/media/aj9/Juniper13/Books/Cookbooks/Keeping/) got scanned by
cookbook_inventory.py as if it held a second, separate set of books, even
though every file in it was already registered under the folder above it
-- so each one got OCR'd and compressed all over again, showing up as an
exact duplicate (see find_duplicate_books.py, which is what surfaced
this). The folder itself has since been removed, but the duplicate rows
-- and the now-redundant OCR'd/compressed copies they point at -- are
still there.

Deleting only from `inventory` and leaving `ocr_results`/`compress_results`
behind would recreate exactly the "ghost row" problem prune_ghosts.py was
written for, so this walks the same original-path -> OCR output ->
compressed output chain /books/view and find_duplicate_books.py use, and
removes all three tables' rows together.

This never touches recipes.db. If a recipe was extracted from one of
these paths, its row is untouched -- recipes.db has no foreign key into
inventory.sqlite (see "The two databases" in ARCHITECTURE.md) -- it will
just stop resolving to a manually-set title/author on /authors and fall
back to the historical source_book parse, same as any book with no
inventory match. The report below flags how many matched rows already
had recipes extracted, so you can sanity-check that before deleting.

SAFE BY DEFAULT: running this with no flags only *reports* what it would
delete. Nothing is removed from the database until you pass --apply, and
files on disk are only ever touched with --apply --delete-files together
-- and even then, only the OCR'd/compressed copies these rows point at,
never the original scanned file itself.

Usage:
    # 1. Always look first -- prints exactly what would be removed.
    python3 remove_books_by_path_prefix.py "/media/aj9/Juniper13/Books/Cookbooks/Keeping/"

    # 2. Once the report looks right, actually delete the DB rows.
    python3 remove_books_by_path_prefix.py "/media/aj9/Juniper13/Books/Cookbooks/Keeping/" --apply

    # 3. (Optional, separate step) also delete the now-orphaned OCR'd/
    #    compressed FILES those rows pointed at, to reclaim disk space.
    #    Off by default since file deletion can't be undone.
    python3 remove_books_by_path_prefix.py "/media/aj9/Juniper13/Books/Cookbooks/Keeping/" --apply --delete-files
"""

import argparse
import os
import sqlite3


def find_matching_chain(cursor, prefix):
    """Every inventory row under `prefix`, with its ocr_results/
    compress_results output paths if it has them -- unconditionally, not
    filtered by status, since a *failed* OCR/compress attempt still left
    a row (and possibly a file) behind that needs cleaning up too."""
    like_pattern = prefix.rstrip("/") + "/%"
    cursor.execute("""
        SELECT i.path, i.filename, i.recipes_extracted,
               o.output_path AS ocr_output_path,
               c.output_path AS compressed_output_path
        FROM inventory i
        LEFT JOIN ocr_results o ON i.path = o.path
        LEFT JOIN compress_results c ON c.path = COALESCE(o.output_path, i.path)
        WHERE i.path LIKE ?
    """, (like_pattern,))
    return [dict(row) for row in cursor.fetchall()]


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("prefix", help="Folder path prefix to remove, e.g. /media/.../Keeping/")
    parser.add_argument("--db", default=os.path.expanduser("~/cookbook-project/inventory.sqlite"))
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually delete the matching DB rows (default: dry run, report only)",
    )
    parser.add_argument(
        "--delete-files", action="store_true",
        help="Also delete the orphaned OCR'd/compressed files on disk (requires --apply)",
    )
    args = parser.parse_args()

    if args.delete_files and not args.apply:
        parser.error("--delete-files requires --apply")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    matches = find_matching_chain(cursor, args.prefix)

    if not matches:
        print(f"No inventory rows found under prefix: {args.prefix}")
        conn.close()
        return

    ocr_count = sum(1 for m in matches if m["ocr_output_path"])
    compress_count = sum(1 for m in matches if m["compressed_output_path"])
    extracted_count = sum(1 for m in matches if m["recipes_extracted"])

    print(f"{len(matches)} inventory row(s) match prefix: {args.prefix}")
    print(f"  -> {ocr_count} have an ocr_results row")
    print(f"  -> {compress_count} have a compress_results row")
    if extracted_count:
        print(f"  -> {extracted_count} are flagged recipes_extracted=1 "
              f"(their recipes.db rows are untouched either way -- see docstring above)")
    print()
    for m in matches:
        print(f"  {m['path']}")
        if m["ocr_output_path"]:
            print(f"      ocr:        {m['ocr_output_path']}")
        if m["compressed_output_path"]:
            print(f"      compressed: {m['compressed_output_path']}")
    print()

    if not args.apply:
        print("DRY RUN -- nothing deleted. Re-run with --apply to actually delete these rows.")
        conn.close()
        return

    inventory_paths = [m["path"] for m in matches]
    # Same key each compress_results row was joined on above: the OCR
    # output path if this book went through OCR, else the original path
    # directly (natively-compressed books never have an ocr_results row).
    compress_keys = [m["ocr_output_path"] or m["path"] for m in matches]

    def delete_where_in(table, column, values):
        if not values:
            return 0
        placeholders = ",".join("?" * len(values))
        cursor.execute(f"DELETE FROM {table} WHERE {column} IN ({placeholders})", values)
        return cursor.rowcount

    compress_deleted = delete_where_in("compress_results", "path", compress_keys)
    ocr_deleted = delete_where_in("ocr_results", "path", inventory_paths)
    inventory_deleted = delete_where_in("inventory", "path", inventory_paths)
    conn.commit()

    print(f"Deleted {inventory_deleted} inventory row(s), {ocr_deleted} ocr_results row(s), "
          f"{compress_deleted} compress_results row(s).")

    if args.delete_files:
        # Belt-and-braces: don't delete a file if some OTHER row we did
        # NOT just remove still points at that same output path.
        def still_referenced(file_path):
            cursor.execute("SELECT 1 FROM ocr_results WHERE output_path = ?", (file_path,))
            if cursor.fetchone():
                return True
            cursor.execute("SELECT 1 FROM compress_results WHERE output_path = ?", (file_path,))
            return cursor.fetchone() is not None

        candidate_files = set()
        for m in matches:
            for file_path in (m["ocr_output_path"], m["compressed_output_path"]):
                if file_path:
                    candidate_files.add(file_path)

        deleted_files, missing_files, skipped_shared = 0, 0, 0
        for file_path in candidate_files:
            if still_referenced(file_path):
                skipped_shared += 1
                print(f"  SKIPPED (still referenced by another row): {file_path}")
                continue
            if os.path.exists(file_path):
                os.remove(file_path)
                deleted_files += 1
            else:
                missing_files += 1

        print(f"\nDeleted {deleted_files} file(s) from disk "
              f"({missing_files} were already missing, {skipped_shared} skipped as still-referenced).")

    conn.close()


if __name__ == "__main__":
    main()
