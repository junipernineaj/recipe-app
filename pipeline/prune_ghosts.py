import sqlite3

paths = []
with open("/tmp/ghost_rows.txt") as f:
    for line in f:
        line = line.rstrip("\n")
        if line.startswith("SAFE_TO_PRUNE|"):
            paths.append(line.split("|", 1)[1])

conn = sqlite3.connect("/home/aj9/cookbook-project/inventory.sqlite")
cur = conn.cursor()
before = conn.total_changes
cur.executemany("DELETE FROM inventory WHERE path = ?", [(p,) for p in paths])
conn.commit()
after = conn.total_changes
print(f"Attempted to prune {len(paths)} ghost rows, actually deleted {after - before}")
conn.close()
