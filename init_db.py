import sqlite3

conn = sqlite3.connect("recipes.db")
cursor = conn.cursor()

cursor.execute("""
    CREATE TABLE IF NOT EXISTS recipes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        source_book TEXT
    )
""")

cursor.execute(
    "INSERT INTO recipes (title, source_book) VALUES (?, ?)",
    ("Test Recipe One", "My Test Cookbook")
)
cursor.execute(
    "INSERT INTO recipes (title, source_book) VALUES (?, ?)",
    ("Test Recipe Two", "My Test Cookbook")
)

conn.commit()
conn.close()

print("Database created and seeded.")
