#!/usr/bin/env python3
"""
Phase 1: Cookbook library inventory / readability triage.

Walks a directory of scanned cookbooks (PDF, EPUB, DOCX, RTF, TXT, and
assorted ebook formats), tries to extract text from each one, and records
whether each file is:

  - readable_native      : plain-text extraction worked directly (PDF/EPUB/
                            DOCX/RTF/TXT) and found a healthy amount of text
  - readable_via_calibre : format needed Calibre's ebook-convert to get text
                            out (mobi/azw/azw3/fb2/lit/djvu/odt/doc/html/chm)
  - needs_ocr            : file opened fine but text extraction returned
                            little/nothing -> likely an image-only scan that
                            never actually got OCR'd (or OCR failed on it)
  - encrypted            : PDF is password-protected, needs a password first
  - unsupported          : extension not handled and Calibre isn't installed
                            (or Calibre itself failed on it)
  - error                : file wouldn't open / parse at all (corrupt, etc.)

Results go into a SQLite database (the seed of the eventual recipe DB) and
are also dumped to a CSV for easy eyeballing. This step does NOT use the
GPU or any LLM -- it's pure text extraction, just to map the playing field
before Phase 2 (structured recipe extraction) runs.

Usage:
    source venv/bin/activate
    python cookbook_inventory.py --source /path/to/cookbooks \
        --db inventory.sqlite --csv inventory.csv

    # quick dry run on the first 20 files found
    python cookbook_inventory.py --source /path/to/cookbooks --sample 20

    # re-run later and only process files not already recorded
    python cookbook_inventory.py --source /path/to/cookbooks --resume
"""

import argparse
import csv
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# --- optional deps, degrade gracefully -------------------------------------
try:
    import pymupdf as fitz  # PyMuPDF (new import name; falls back to old one below)
except ImportError:
    try:
        import fitz
    except ImportError:
        fitz = None

try:
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup
except ImportError:
    ebooklib = None

try:
    import docx  # python-docx
except ImportError:
    docx = None

try:
    from striprtf.striprtf import rtf_to_text
except ImportError:
    rtf_to_text = None

try:
    import chardet
except ImportError:
    chardet = None

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable

# ---------------------------------------------------------------------------

NATIVE_EXTS = {".pdf", ".epub", ".docx", ".rtf", ".txt"}
CALIBRE_EXTS = {
    ".mobi", ".azw", ".azw3", ".azw4", ".fb2", ".lit", ".lrf",
    ".pdb", ".djvu", ".doc", ".odt", ".htm", ".html", ".chm",
}
ALL_HANDLED_EXTS = NATIVE_EXTS | CALIBRE_EXTS

# below this many extracted characters per page/chapter, treat as "no real text"
MIN_CHARS_PER_UNIT = 40
# hard timeout for a single calibre conversion, seconds
CALIBRE_TIMEOUT = 180

CALIBRE_AVAILABLE = shutil.which("ebook-convert") is not None


def handle_pdf(path: Path):
    if fitz is None:
        return "error", 0, 0, "PyMuPDF not installed"
    try:
        doc = fitz.open(path)
    except Exception as e:
        msg = str(e)
        if "password" in msg.lower() or "encrypt" in msg.lower():
            return "encrypted", 0, 0, msg
        return "error", 0, 0, msg

    if doc.is_encrypted:
        # is_encrypted stays true even after a blank-password open attempt fails
        if not doc.authenticate(""):
            try:
                page_count = doc.page_count
            except Exception:
                page_count = 0
            doc.close()
            return "encrypted", 0, page_count, "PDF requires a password"

    total_chars = 0
    page_count = doc.page_count
    try:
        for page in doc:
            total_chars += len(page.get_text().strip())
    except Exception as e:
        doc.close()
        return "error", 0, page_count, str(e)
    doc.close()

    if page_count == 0:
        return "error", 0, 0, "zero pages"
    avg = total_chars / page_count
    if avg < MIN_CHARS_PER_UNIT:
        return "needs_ocr", total_chars, page_count, None
    return "readable_native", total_chars, page_count, None


def handle_epub(path: Path):
    if ebooklib is None:
        return "error", 0, 0, "ebooklib not installed"
    try:
        book = epub.read_epub(str(path))
    except Exception as e:
        return "error", 0, 0, str(e)

    total_chars = 0
    unit_count = 0
    try:
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            soup = BeautifulSoup(item.get_content(), "html.parser")
            text = soup.get_text().strip()
            if text:
                unit_count += 1
                total_chars += len(text)
    except Exception as e:
        return "error", total_chars, unit_count, str(e)

    if unit_count == 0:
        return "needs_ocr", total_chars, 0, None
    avg = total_chars / unit_count
    if avg < MIN_CHARS_PER_UNIT:
        return "needs_ocr", total_chars, unit_count, None
    return "readable_native", total_chars, unit_count, None


def handle_docx(path: Path):
    if docx is None:
        return "error", 0, 0, "python-docx not installed"
    try:
        d = docx.Document(str(path))
    except Exception as e:
        return "error", 0, 0, str(e)
    paras = [p.text for p in d.paragraphs if p.text.strip()]
    total_chars = sum(len(p) for p in paras)
    unit_count = max(len(paras), 1)
    if total_chars / unit_count < MIN_CHARS_PER_UNIT and total_chars < 200:
        return "needs_ocr", total_chars, unit_count, None
    return "readable_native", total_chars, unit_count, None


def handle_rtf(path: Path):
    if rtf_to_text is None:
        return "error", 0, 0, "striprtf not installed"
    try:
        raw = path.read_text(errors="ignore")
        text = rtf_to_text(raw)
    except Exception as e:
        return "error", 0, 0, str(e)
    total_chars = len(text.strip())
    if total_chars < 200:
        return "needs_ocr", total_chars, 1, None
    return "readable_native", total_chars, 1, None


def handle_txt(path: Path):
    try:
        raw = path.read_bytes()
        if chardet is not None:
            enc = chardet.detect(raw).get("encoding") or "utf-8"
        else:
            enc = "utf-8"
        text = raw.decode(enc, errors="ignore")
    except Exception as e:
        return "error", 0, 0, str(e)
    total_chars = len(text.strip())
    if total_chars < 200:
        return "needs_ocr", total_chars, 1, None
    return "readable_native", total_chars, 1, None


def handle_via_calibre(path: Path):
    if not CALIBRE_AVAILABLE:
        return "unsupported", 0, 0, "calibre (ebook-convert) not installed"
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "out.txt"
        try:
            subprocess.run(
                ["ebook-convert", str(path), str(out_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=CALIBRE_TIMEOUT,
                check=True,
            )
        except subprocess.TimeoutExpired:
            return "error", 0, 0, "calibre conversion timed out"
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode(errors="ignore") if e.stderr else str(e)
            return "unsupported", 0, 0, stderr[:500]

        if not out_path.exists():
            return "unsupported", 0, 0, "calibre produced no output"
        try:
            text = out_path.read_text(errors="ignore")
        except Exception as e:
            return "error", 0, 0, str(e)

    total_chars = len(text.strip())
    # rough "chapter" proxy: count of blank-line-separated blocks
    unit_count = max(text.count("\n\n"), 1)
    if total_chars / unit_count < MIN_CHARS_PER_UNIT and total_chars < 500:
        return "needs_ocr", total_chars, unit_count, None
    return "readable_via_calibre", total_chars, unit_count, None


def classify(path: Path):
    ext = path.suffix.lower()
    if ext == ".pdf":
        return handle_pdf(path)
    if ext == ".epub":
        return handle_epub(path)
    if ext == ".docx":
        return handle_docx(path)
    if ext == ".rtf":
        return handle_rtf(path)
    if ext == ".txt":
        return handle_txt(path)
    if ext in CALIBRE_EXTS:
        return handle_via_calibre(path)
    return "unsupported", 0, 0, f"unhandled extension {ext}"


def init_db(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inventory (
            path TEXT PRIMARY KEY,
            filename TEXT,
            extension TEXT,
            size_bytes INTEGER,
            status TEXT,
            unit_count INTEGER,
            extracted_chars INTEGER,
            avg_chars_per_unit REAL,
            error_message TEXT,
            scanned_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def already_scanned(conn, path_str):
    cur = conn.execute("SELECT 1 FROM inventory WHERE path = ? AND status != 'error'", (path_str,))
    return cur.fetchone() is not None


def export_csv(conn, csv_path: Path):
    cur = conn.execute(
        "SELECT path, filename, extension, size_bytes, status, unit_count, "
        "extracted_chars, avg_chars_per_unit, error_message, scanned_at "
        "FROM inventory ORDER BY status, path"
    )
    rows = cur.fetchall()
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "path", "filename", "extension", "size_bytes", "status",
                "unit_count", "extracted_chars", "avg_chars_per_unit",
                "error_message", "scanned_at",
            ]
        )
        w.writerows(rows)


def print_summary(conn):
    print("\n=== Phase 1 summary ===")
    cur = conn.execute(
        "SELECT status, COUNT(*), SUM(size_bytes) FROM inventory GROUP BY status ORDER BY COUNT(*) DESC"
    )
    total = 0
    for status, count, size in cur.fetchall():
        total += count
        size_mb = (size or 0) / (1024 * 1024)
        print(f"  {status:22s} {count:6d} files   ({size_mb:,.1f} MB)")
    print(f"  {'TOTAL':22s} {total:6d} files")

    print("\n  by extension:")
    cur = conn.execute(
        "SELECT extension, status, COUNT(*) FROM inventory GROUP BY extension, status ORDER BY extension"
    )
    for ext, status, count in cur.fetchall():
        print(f"    {ext:8s} {status:22s} {count:6d}")

    if not CALIBRE_AVAILABLE:
        print(
            "\n  NOTE: calibre's ebook-convert was not found on this machine, "
            "so any mobi/azw/azw3/fb2/lit/djvu/odt/doc/html/chm files were "
            "marked 'unsupported' rather than actually tried. Install calibre "
            "and re-run with --resume to pick those up."
        )


def main():
    ap = argparse.ArgumentParser(description="Phase 1: cookbook library inventory / readability triage")
    ap.add_argument("--source", required=True, help="root directory to scan recursively")
    ap.add_argument("--db", default="inventory.sqlite", help="sqlite db path (default: inventory.sqlite)")
    ap.add_argument("--csv", default="inventory.csv", help="csv export path (default: inventory.csv)")
    ap.add_argument("--sample", type=int, default=0, help="only process the first N files found (dry run)")
    ap.add_argument("--resume", action="store_true", help="skip files already recorded in the db")
    ap.add_argument("--commit-every", type=int, default=20, help="db commit batch size (default 20)")
    args = ap.parse_args()

    source = Path(args.source).expanduser()
    if not source.is_dir():
        print(f"Source directory not found: {source}", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning {source} recursively...")
    everything = [p for p in source.rglob("*") if p.is_file()]
    candidate_files = [p for p in everything if p.suffix.lower() in ALL_HANDLED_EXTS]
    other_files = [p for p in everything if p.suffix.lower() not in ALL_HANDLED_EXTS]
    print(
        f"Found {len(everything)} files total: {len(candidate_files)} in recognised "
        f"ebook/document formats, {len(other_files)} with other extensions "
        f"(these still get recorded as 'unsupported_extension' so nothing is left off the map)"
    )

    if args.sample:
        candidate_files = candidate_files[: args.sample]
        other_files = other_files[: args.sample]
        print(f"--sample set: only processing first {len(candidate_files)} candidate + {len(other_files)} other files")

    conn = init_db(Path(args.db))

    if not CALIBRE_AVAILABLE:
        print("WARNING: 'ebook-convert' not found on PATH -- calibre-only formats will be marked unsupported until it's installed.")

    def record(path, status, chars, units, err):
        path_str = str(path)
        if args.resume and already_scanned(conn, path_str):
            return False
        try:
            size_bytes = path.stat().st_size
        except OSError:
            size_bytes = 0
        avg = (chars / units) if units else 0
        conn.execute(
            """
            INSERT INTO inventory
                (path, filename, extension, size_bytes, status, unit_count,
                 extracted_chars, avg_chars_per_unit, error_message, scanned_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(path) DO UPDATE SET
                filename=excluded.filename, extension=excluded.extension,
                size_bytes=excluded.size_bytes, status=excluded.status,
                unit_count=excluded.unit_count, extracted_chars=excluded.extracted_chars,
                avg_chars_per_unit=excluded.avg_chars_per_unit,
                error_message=excluded.error_message, scanned_at=excluded.scanned_at
            """,
            (path_str, path.name, path.suffix.lower(), size_bytes, status, units, chars, avg, err),
        )
        return True

    # cheap pass: record files with extensions we don't even attempt, no need to open them
    since_commit = 0
    for path in tqdm(other_files, desc="Recording other files", unit="file"):
        if record(path, "unsupported_extension", 0, 0, f"unrecognised extension {path.suffix.lower() or '(none)'}"):
            since_commit += 1
            if since_commit >= args.commit_every:
                conn.commit()
                since_commit = 0
    conn.commit()

    since_commit = 0
    for path in tqdm(candidate_files, desc="Scanning", unit="file"):
        if args.resume and already_scanned(conn, str(path)):
            continue  # skip re-opening/re-parsing a file we already have a result for
        status, chars, units, err = classify(path)
        if record(path, status, chars, units, err):
            since_commit += 1
            if since_commit >= args.commit_every:
                conn.commit()
                since_commit = 0

    conn.commit()
    export_csv(conn, Path(args.csv))
    print_summary(conn)
    print(f"\nWrote {args.db} and {args.csv}")
    conn.close()


if __name__ == "__main__":
    main()
