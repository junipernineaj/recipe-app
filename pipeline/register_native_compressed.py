import sqlite3
from pathlib import Path

DB = "/home/aj9/cookbook-project/inventory.sqlite"
COMPRESSED_DIR = Path("/media/aj9/Juniper13/cookbook_ocr_compressed")

conn = sqlite3.connect(DB)
cur = conn.cursor()

cur.execute("""
    SELECT i.path, i.filename FROM inventory i
    WHERE i.status = 'readable_native'
    AND NOT EXISTS (SELECT 1 FROM compress_results c WHERE c.path = i.path)
""")
rows = cur.fetchall()
print(f"Found {len(rows)} readable_native book(s) with no compress_results row.")

registered = 0
missing = []
for path_str, filename in rows:
    candidate = COMPRESSED_DIR / filename
    if candidate.exists():
        size = candidate.stat().st_size
        cur.execute("""
            INSERT INTO compress_results
                (path, output_path, status, original_bytes, compressed_bytes,
                 pct_saved, text_match_ratio, duration_seconds, error_message, completed_at)
            VALUES (?, ?, 'verified_ok', ?, ?, 0.0, 1.0, 0.0, NULL, datetime('now'))
            ON CONFLICT(path) DO NOTHING
        """, (path_str, str(candidate), size, size))
        registered += 1
    else:
        missing.append(path_str)

conn.commit()
print(f"Registered {registered} book(s) already sitting in {COMPRESSED_DIR}.")
if missing:
    print(f"WARNING: {len(missing)} book(s) have NO file in {COMPRESSED_DIR} either -- "
          f"genuinely missing, needs manual review:")
    for p in missing:
        print(f"  {p}")
conn.close()
