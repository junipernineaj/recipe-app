import sqlite3
from pathlib import Path

DB = "/home/aj9/cookbook-project/inventory.sqlite"

conn = sqlite3.connect(DB)
cur = conn.cursor()

cur.execute("""
    SELECT o.path, o.output_path, o.completed_at
    FROM ocr_results o
    LEFT JOIN inventory i ON i.path = o.path
    WHERE i.path IS NULL
""")
rows = cur.fetchall()

restored = 0
for path_str, output_path, completed_at in rows:
    p = Path(path_str)
    size_bytes = 0
    if output_path and Path(output_path).exists():
        size_bytes = Path(output_path).stat().st_size
    cur.execute("""
        INSERT INTO inventory (path, filename, extension, size_bytes, status, scanned_at, excluded)
        VALUES (?, ?, ?, ?, 'ocr_done', ?, 0)
        ON CONFLICT(path) DO NOTHING
    """, (path_str, p.name, p.suffix.lstrip('.'), size_bytes, completed_at))
    if cur.rowcount:
        restored += 1

conn.commit()
print(f"Re-inserted {restored} missing inventory row(s) out of {len(rows)} checked.")
conn.close()
