import sqlite3

conn = sqlite3.connect("recipes.db")
cursor = conn.cursor()

cursor.execute("DROP TABLE IF EXISTS recipes")

cursor.execute("""
    CREATE TABLE recipes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        source_book TEXT,
        ingredients TEXT,
        instructions TEXT
    )
""")

cursor.execute(
    "INSERT INTO recipes (title, source_book, ingredients, instructions) VALUES (?, ?, ?, ?)",
    (
        "Test Recipe One",
        "My Test Cookbook",
        "2 eggs\n1 cup flour\n1/2 cup sugar",
        "Mix dry ingredients.\nWhisk in eggs.\nBake at 180C for 20 minutes."
    )
)
cursor.execute(
    "INSERT INTO recipes (title, source_book, ingredients, instructions) VALUES (?, ?, ?, ?)",
    (
        "Test Recipe Two",
        "My Test Cookbook",
        "1 onion\n2 cloves garlic\n400g tomatoes",
        "Fry onion and garlic.\nAdd tomatoes and simmer 15 minutes."
    )
)

conn.commit()
conn.close()

print("Database recreated and seeded.")
