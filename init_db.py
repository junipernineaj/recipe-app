import sqlite3

conn = sqlite3.connect("recipes.db")
cursor = conn.cursor()

cursor.execute("DROP TABLE IF EXISTS recipes")

cursor.execute("""
    CREATE TABLE recipes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        source_book TEXT,
        source_path TEXT,
        source_page INTEGER,
        ingredients TEXT,
        instructions TEXT
    )
""")

cursor.execute(
    "INSERT INTO recipes (title, source_book, source_path, source_page, ingredients, instructions) VALUES (?, ?, ?, ?, ?, ?)",
    (
        "Test Recipe One",
        "My Test Cookbook",
        "My Test Cookbook.pdf",
        12,
        "2 eggs\n1 cup flour\n1/2 cup sugar",
        "Mix dry ingredients.\nWhisk in eggs.\nBake at 180C for 20 minutes."
    )
)
cursor.execute(
    "INSERT INTO recipes (title, source_book, source_path, source_page, ingredients, instructions) VALUES (?, ?, ?, ?, ?, ?)",
    (
        "Test Recipe Two",
        "My Test Cookbook",
        "My Test Cookbook.pdf",
        45,
        "1 onion\n2 cloves garlic\n400g tomatoes",
        "Fry onion and garlic.\nAdd tomatoes and simmer 15 minutes."
    )
)

conn.commit()
conn.close()

print("Database recreated and seeded.")
