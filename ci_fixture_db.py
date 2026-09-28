"""Builds two small, throwaway databases for CI: recipes.db, and the
pipeline's inventory.sqlite (the app touches both -- importing main.py
alone, before any route is even hit, initializes a column on
inventory.sqlite, and the /books route joins three of its tables). Neither
database's real schema lives anywhere in code as a single source of truth:
it's grown by hand over time (an ALTER TABLE here, a PRAGMA-checked column
there), so this fixture is hand-maintained too, reusing the real
table-creation functions from pipeline/ wherever one exists rather than
re-declaring their schemas.

This is CI-only. It refuses to run if either database already exists,
specifically so it can never be run by accident against real data.

If a future feature selects a column that isn't set up below, CI will fail
with a clear "no such column" error rather than silently passing; when that
happens, just add the column here too.
"""
import os
import sqlite3
import sys
from pathlib import Path

RECIPES_DB_PATH = Path("recipes.db")
INVENTORY_DB_PATH = Path(os.path.expanduser("~/cookbook-project/inventory.sqlite"))

INGREDIENTS = """2 eggs
1 cup flour
1/2 cup sugar"""

INSTRUCTIONS = """Mix the flour and sugar.
Whisk in the eggs.
Bake at 180C for 20 minutes."""


def build_recipes_db():
    conn = sqlite3.connect(RECIPES_DB_PATH)
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
            flagged_for_review TEXT,
            extracted_at TEXT
        )
    """)
    conn.execute(
        """INSERT INTO recipes
           (title, source_book, source_path, source_page, ingredients, instructions, status, engine, extracted_at)
           VALUES (?, ?, ?, ?, ?, ?, 'approved', 'ci-fixture', datetime('now'))""",
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
    from pipeline.recipe_qc import init_qc_table, run_checks

    init_qc_table(conn)
    recipe_id = conn.execute("SELECT id FROM recipes LIMIT 1").fetchone()[0]
    run_checks(conn, recipe_id)
    conn.close()
    print(f"Seeded {RECIPES_DB_PATH} with one fixture recipe for CI.")


def build_inventory_db():
    from pipeline.cookbook_inventory import init_db as init_inventory_table
    from pipeline.ocr_pass import init_results_table as init_ocr_results_table
    from pipeline.compress_pass import init_results_table as init_compress_results_table

    INVENTORY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = init_inventory_table(INVENTORY_DB_PATH)

    # 'excluded' isn't in cookbook_inventory.py's own CREATE TABLE -- like
    # recipes.status, it was added by hand on the real database at some
    # point. 'recipes_extracted' doesn't need adding here: main.py adds that
    # column itself the moment it's imported, same as it would for real.
    conn.execute("ALTER TABLE inventory ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")
    conn.commit()

    init_ocr_results_table(conn)
    init_compress_results_table(conn)
    conn.close()
    print(f"Seeded {INVENTORY_DB_PATH} (empty -- just enough schema for /books to load) for CI.")


def main():
    if RECIPES_DB_PATH.exists():
        raise SystemExit(
            f"{RECIPES_DB_PATH} already exists -- refusing to run. This script only "
            "builds throwaway fixture databases for CI."
        )
    if INVENTORY_DB_PATH.exists():
        raise SystemExit(
            f"{INVENTORY_DB_PATH} already exists -- refusing to run. This script only "
            "builds throwaway fixture databases for CI."
        )

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    build_recipes_db()
    build_inventory_db()


if __name__ == "__main__":
    main()
