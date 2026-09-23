# recipe-app

A personal cookbook library and recipe app, running on a home server
(junipernine2), built from two connected pieces:

- **The pipeline** (`pipeline/`) — OCRs and compresses a growing library of
  scanned cookbook PDFs into a searchable, space-efficient archive.
- **The web app** (`main.py`, `templates/`) — a FastAPI + HTMX site for
  browsing that library, and (eventually) individual recipes extracted
  from it.

Both share one SQLite database at `~/cookbook-project/inventory.sqlite`.

**Start here:** [`documentation/ARCHITECTURE.md`](documentation/ARCHITECTURE.md)
covers where everything lives, how the pipeline stages fit together, the
database schema, and the gotchas that have come up along the way. Read that
before touching the pipeline scripts — it'll save you rediscovering the
same footguns twice.

## Quick reference

| What | Where |
|---|---|
| New scans go here | `/media/aj9/Juniper13/Books/Cookbooks/` |
| Permanent OCR'd archive | `/media/aj9/Juniper13/cookbook_ocr_output/` |
| Compressed copies the app serves | `/media/aj9/Juniper13/cookbook_ocr_compressed/` |
| Shared database | `~/cookbook-project/inventory.sqlite` |
| Web app venv | `~/recipe-app/venv` |
| Pipeline venv | `~/cookbook-project/venv` |
| Live site | https://recipes.junipernine.com |

Run the app:

```
cd ~/recipe-app
source venv/bin/activate
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Run the pipeline (from `~/recipe-app/pipeline`, with `~/cookbook-project/venv`
activated) — see [`documentation/ARCHITECTURE.md`](documentation/ARCHITECTURE.md)
for the full commands and flags:

```
python3 cookbook_inventory.py --source /media/aj9/Juniper13/Books/Cookbooks --db ~/cookbook-project/inventory.sqlite --resume
python3 ocr_pass.py --db ~/cookbook-project/inventory.sqlite --source /media/aj9/Juniper13/Books/Cookbooks --output-dir /media/aj9/Juniper13/cookbook_ocr_output --resume
python3 compress_pass.py --db ~/cookbook-project/inventory.sqlite --source /media/aj9/Juniper13/cookbook_ocr_output --output-dir /media/aj9/Juniper13/cookbook_ocr_compressed --resume
```
