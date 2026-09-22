#!/usr/bin/env python3
"""
Phase 1.5: OCR pass over the files Phase 1 flagged as `needs_ocr`.

Reads inventory.sqlite (produced by cookbook_inventory.py), takes every PDF
recorded with status='needs_ocr', and runs ocrmypdf on it to bake a
searchable text layer back in. Originals are NEVER modified in place -- the
OCR'd copy is written to a separate output directory, mirroring the same
relative folder structure as the source, so your original scans stay
untouched no matter what happens.

After each file is OCR'd, this script re-extracts text from the new file
(same logic as Phase 1) to actually verify OCR worked, rather than just
trusting ocrmypdf's exit code -- a scan can be too faint/skewed/handwritten
for Tesseract to get much out of it even when the tool "succeeds". Results
go into a new `ocr_results` table in the same db, so you end up with:

  ocr_success_verified   : ocrmypdf ran and the output now has real text
  ocr_ran_low_text       : ocrmypdf ran without error, but text is still
                            below threshold -- likely a genuinely hard scan
                            (faint print, handwriting, heavy stains) that
                            needs a human look, not just a re-run
  failed                 : ocrmypdf itself errored out (bad exit code)
  skipped_not_pdf        : needs_ocr file that isn't a PDF (ocrmypdf only
                            handles PDFs) -- flagged for manual handling

Usage:
    source venv/bin/activate
    python ocr_pass.py --db inventory.sqlite --source /path/to/cookbooks \
        --output-dir ~/cookbook-project/ocr_output

    # quick test on the first 5 files
    python ocr_pass.py --db inventory.sqlite --source /path/to/cookbooks \
        --output-dir ~/cookbook-project/ocr_output --limit 5

    # re-run later, skipping files already processed
    python ocr_pass.py --db inventory.sqlite --source /path/to/cookbooks \
        --output-dir ~/cookbook-project/ocr_output --resume
"""

import argparse
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz
    except ImportError:
        fitz = None

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable

MIN_CHARS_PER_PAGE = 40  # same threshold Phase 1 used
OCRMYPDF_AVAILABLE = shutil.which("ocrmypdf") is not None


def init_results_table(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ocr_results (
            path TEXT PRIMARY KEY,
            output_path TEXT,
            ocr_status TEXT,
            verified_pages INTEGER,
            verified_chars INTEGER,
            verified_avg_per_page REAL,
            duration_seconds REAL,
            error_message TEXT,
            completed_at TEXT
        )
        """
    )
    conn.commit()


def already_done(conn, path_str):
    cur = conn.execute("SELECT 1 FROM ocr_results WHERE path = ?", (path_str,))
    return cur.fetchone() is not None


def record_result(conn, path_str, output_path, status, pages, chars, avg, duration, err):
    conn.execute(
        """
        INSERT INTO ocr_results
            (path, output_path, ocr_status, verified_pages, verified_chars,
             verified_avg_per_page, duration_seconds, error_message, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(path) DO UPDATE SET
            output_path=excluded.output_path, ocr_status=excluded.ocr_status,
            verified_pages=excluded.verified_pages, verified_chars=excluded.verified_chars,
            verified_avg_per_page=excluded.verified_avg_per_page,
            duration_seconds=excluded.duration_seconds,
            error_message=excluded.error_message, completed_at=excluded.completed_at
        """,
        (path_str, str(output_path), status, pages, chars, avg, duration, err),
    )


def verify_text(pdf_path: Path):
    """Re-extract text from an OCR'd PDF to confirm it actually worked."""
    if fitz is None:
        return 0, 0, 0.0, "PyMuPDF not installed, cannot verify"
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        return 0, 0, 0.0, f"could not open OCR output: {e}"
    total_chars = 0
    page_count = doc.page_count
    for page in doc:
        total_chars += len(page.get_text().strip())
    doc.close()
    avg = total_chars / page_count if page_count else 0
    return page_count, total_chars, avg, None


def run_ocrmypdf(input_path: Path, output_path: Path, lang: str, jobs: int, clean: bool, timeout: int):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ocrmypdf",
        "--skip-text",       # don't touch pages that already have usable text
        "--deskew",
        "--rotate-pages",
        "--language", lang,
        "--jobs", str(jobs),
        "--output-type", "pdf",
    ]
    if clean:
        cmd.append("--clean")
    cmd += [str(input_path), str(output_path)]

    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, text=True,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"

    if result.returncode != 0:
        # ocrmypdf's own message is usually informative; keep it but bound the size
        return False, (result.stdout or "")[-1500:]
    return True, None


def main():
    ap = argparse.ArgumentParser(description="Phase 1.5: OCR pass over needs_ocr files")
    ap.add_argument("--db", required=True, help="inventory.sqlite path from Phase 1")
    ap.add_argument("--source", required=True, help="same source root used in Phase 1 (for computing relative paths)")
    ap.add_argument("--output-dir", required=True, help="where OCR'd copies get written (mirrors source structure)")
    ap.add_argument("--lang", default="eng", help="tesseract language code(s), e.g. eng or eng+fra (default: eng)")
    ap.add_argument("--jobs", type=int, default=4, help="parallel page-OCR jobs per file (default: 4)")
    ap.add_argument("--clean", action="store_true", help="run unpaper cleanup before OCR (slower, can help noisy scans)")
    ap.add_argument("--timeout", type=int, default=2400, help="per-file timeout in seconds (default: 2400 = 40 min)")
    ap.add_argument("--limit", type=int, default=0, help="only process the first N files (dry run)")
    ap.add_argument("--resume", action="store_true", help="skip files already recorded in ocr_results")
    args = ap.parse_args()

    if not OCRMYPDF_AVAILABLE:
        print("ERROR: 'ocrmypdf' not found on PATH. Install it first, e.g.:", file=sys.stderr)
        print("  sudo apt install -y ocrmypdf tesseract-ocr ghostscript qpdf unpaper", file=sys.stderr)
        sys.exit(1)
    if fitz is None:
        print("ERROR: PyMuPDF not installed in this venv (needed to verify OCR results).", file=sys.stderr)
        sys.exit(1)

    source = Path(args.source).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(args.db)
    init_results_table(conn)

    cur = conn.execute("SELECT path, extension FROM inventory WHERE status = 'needs_ocr' AND excluded = 0 ORDER BY path")
    rows = cur.fetchall()
    print(f"Found {len(rows)} files marked needs_ocr in {args.db}")

    if args.limit:
        rows = rows[: args.limit]
        print(f"--limit set: only processing first {len(rows)}")

    pdf_rows, non_pdf_rows = [], []
    for path_str, ext in rows:
        (pdf_rows if ext == ".pdf" else non_pdf_rows).append(path_str)

    if non_pdf_rows:
        print(f"{len(non_pdf_rows)} needs_ocr file(s) aren't PDFs -- ocrmypdf can't handle those, recording as skipped_not_pdf")
        for path_str in non_pdf_rows:
            if args.resume and already_done(conn, path_str):
                continue
            record_result(conn, path_str, "", "skipped_not_pdf", 0, 0, 0.0, 0.0, "not a PDF")
        conn.commit()

    stats = {"ocr_success_verified": 0, "ocr_ran_low_text": 0, "failed": 0, "skipped": 0}

    since_commit = 0
    for path_str in tqdm(pdf_rows, desc="OCR", unit="file"):
        if args.resume and already_done(conn, path_str):
            stats["skipped"] += 1
            continue

        input_path = Path(path_str)
        try:
            rel = input_path.resolve().relative_to(source)
        except ValueError:
            rel = Path(input_path.name)
        output_path = output_dir / rel

        t0 = time.time()
        ok, err = run_ocrmypdf(input_path, output_path, args.lang, args.jobs, args.clean, args.timeout)
        duration = time.time() - t0

        if not ok:
            record_result(conn, path_str, output_path, "failed", 0, 0, 0.0, duration, err)
            stats["failed"] += 1
        else:
            pages, chars, avg, verr = verify_text(output_path)
            if verr:
                record_result(conn, path_str, output_path, "failed", pages, chars, avg, duration, verr)
                stats["failed"] += 1
            elif avg >= MIN_CHARS_PER_PAGE:
                record_result(conn, path_str, output_path, "ocr_success_verified", pages, chars, avg, duration, None)
                stats["ocr_success_verified"] += 1
            else:
                record_result(conn, path_str, output_path, "ocr_ran_low_text", pages, chars, avg, duration, None)
                stats["ocr_ran_low_text"] += 1

        since_commit += 1
        if since_commit >= 1:
            conn.commit()
            since_commit = 0

    conn.commit()

    print("\n=== OCR pass summary ===")
    for k, v in stats.items():
        print(f"  {k:24s} {v}")
    print(f"\nOCR'd copies written under: {output_dir}")
    print(f"Results recorded in ocr_results table in: {args.db}")
    if stats["ocr_ran_low_text"]:
        print(
            f"\n{stats['ocr_ran_low_text']} file(s) ran through OCR but still came back with little text -- "
            "these are worth a manual look (query: SELECT path FROM ocr_results WHERE ocr_status='ocr_ran_low_text'). "
            "Could be faint/damaged scans, handwriting, or pages that need --clean / a different --lang."
        )
    conn.close()


if __name__ == "__main__":
    main()
