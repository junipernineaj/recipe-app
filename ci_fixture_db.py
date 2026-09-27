"""Builds a small, throwaway recipes.db for CI -- just enough schema and one
seeded recipe for site_checks.py's app-level checks (the home page, the
authors page, the QC backfill count, etc.) to exercise the real routes and
the real QC pipeline, without needing the real ~700-book library, which only
ever exists on the home server.

This is CI-only. It refuses to run if recipes.db already exists, specifically
so it can never be run by accident against a real database.

The recipes table columns here are hand-maintained -- nothing in main.py
creates that table (it's only ever been created once, by hand, a long time
ago, and altered by hand since). If a future feature selects a column that
isn't listed below, CI will fail with a clear "no such column" error rather
than silently passing; when that happens, just add the column here too.
"""
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path("recipes.db")

INGREDIENTS = """2 eggs
1 cup flour
1/2 cup sugar"""

INSTRUCTIONS = """Mix the flour and sugar.
Whisk in the eggs.
Bake at 180C for 20 minutes."""


def main():
    if DB_PATH.exists():
        raise SystemExit(
            f"{DB_PATH} already exists -- refusing to run. This script only "
            "builds a throwaway fixture database for CI."
        )

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            source_book TEXT,
            source_path TEXT,
            source_page INTEGER,
            ingredients TEXT,
            instructions TEXT,
            status TEXT NOT NULL DEFAULT 'approved',
            engine TEXT,
            flagged_for_review TEXT
        )
    """)
    conn.execute(
        """INSERT INTO recipes
           (title, source_book, source_path, source_page, ingredients, instructions, status, engine)
           VALUES (?, ?, ?, ?, ?, ?, 'approved', 'ci-fixture')""",
        (
            "CI Fixture Recipe",
            "CI Test Author - CI Test Cookbook",
            "ci-fixture.pdf",
            1,
            INGREDIENTS,
            INSTRUCTIONS,
        ),
    )
    conn.commit()

    # Run the real QC pipeline against the seed recipe, so
    # qc_backfill_up_to_date has real, complete rows to check -- exactly as
    # it would for a recipe extracted for real, rather than an empty table.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pipeline.recipe_qc import init_qc_table, run_checks

    init_qc_table(conn)
    recipe_id = conn.execute("SELECT id FROM recipes LIMIT 1").fetchone()[0]
    run_checks(conn, recipe_id)

    conn.close()
    print(f"Seeded {DB_PATH} with one fixture recipe for CI.")


if __name__ == "__main__":
    main()
