#!/usr/bin/env python3
"""
Phase 2: Recompress/downsample the OCR'd PDFs to shrink file size while
preserving the invisible OCR text layer.
 
Why this is safe: ocrmypdf (Phase 1.5) leaves the original scanned page
image in place and adds an *invisible* text layer positioned in the page's
absolute coordinate space (points), not tied to the image's pixel
resolution. Ghostscript's PDF-to-PDF distillation reprocesses image
XObjects (recompresses/downsamples them) but passes text objects straight
through untouched. So shrinking the images does not move or damage the
OCR text -- searching/selecting still works identically afterward.
 
This script:
  - reads every PDF under --source (your OCR'd output directory)
  - runs it through Ghostscript with configurable downsample resolutions
  - writes the result to --output-dir, mirroring the source folder structure
  - verifies the OCR text layer survived by extracting text from both the
    original and the compressed copy (via pdftotext) and comparing them
  - records path, before/after size, % saved, and text-match result in a
    `compress_results` table in the same inventory.sqlite used by Phase 1.5
  - is resumable: --resume skips any file already recorded as a verified
    success (NOT any file with a row -- a prior failure is retried
    automatically, no manual DB surgery needed)
  - pre-sanitizes every file through `qpdf` before Ghostscript touches it.
    Ghostscript's own on-the-fly repair (triggered by things like "invalid
    xref entry, rebuilding xref table") can silently decide a malformed
    file has zero pages and emit an empty, exit-code-0 "success" -- qpdf's
    repair is far more reliable and catches this before it happens. This
    matters most for --file-list runs against un-OCR'd originals (old
    scans, varied producers) since ocrmypdf's own output has already been
    normalized this way.
 
Usage:
    # canary test on ONE specific file first -- always do this before a batch
    python compress_pass.py --db inventory.sqlite \
        --source /media/aj9/Juniper13/cookbook_ocr_output \
        --output-dir /media/aj9/Juniper13/cookbook_ocr_compressed \
        --file "/media/aj9/Juniper13/cookbook_ocr_output/Some Book.pdf"
 
    # full batch, resumable
    python compress_pass.py --db inventory.sqlite \
        --source /media/aj9/Juniper13/cookbook_ocr_output \
        --output-dir /media/aj9/Juniper13/cookbook_ocr_compressed \
        --resume
 
    # targeted mode: process exactly the files listed in a text file (one
    # absolute path per line), regardless of what directory they live in.
    # Useful for natively-readable PDFs that never went through OCR and so
    # never landed under --source's rglob scan -- e.g.:
    #   sqlite3 inventory.sqlite "SELECT path FROM inventory WHERE status='readable_native' AND path LIKE '%.pdf'" > readable_native_pdfs.txt
    #   python compress_pass.py --db inventory.sqlite \
    #       --source /media/aj9/Juniper13/cookbook_ocr_output \
    #       --output-dir /media/aj9/Juniper13/cookbook_ocr_compressed \
    #       --file-list readable_native_pdfs.txt --resume
 
    # tune the DPI targets if the default doesn't hit the size/quality
    # tradeoff you want
    python compress_pass.py --db inventory.sqlite \
        --source /media/aj9/Juniper13/cookbook_ocr_output \
        --output-dir /media/aj9/Juniper13/cookbook_ocr_compressed \
        --color-dpi 150 --gray-dpi 150 --mono-dpi 300 --resume
"""
 
import argparse
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
 
try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable
 
GS_AVAILABLE = shutil.which("gs") is not None
PDFTOTEXT_AVAILABLE = shutil.which("pdftotext") is not None
QPDF_AVAILABLE = shutil.which("qpdf") is not None
 
# Below this similarity ratio between before/after extracted text, flag for
# manual review rather than silently trusting it. This is a word-multiset
# (order-independent) comparison, not a sequence diff -- Ghostscript's
# recompression can restructure a page's content streams enough to change
# pdftotext's reading-order guess even when zero text is actually lost, and
# a sequence-based ratio produces false alarms on that reordering alone.
# Genuine content loss shows up here as a real drop in overlap; harmless
# reordering does not, so this threshold can stay strict.
TEXT_MATCH_THRESHOLD = 0.999
 
 
def init_results_table(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS compress_results (
            path TEXT PRIMARY KEY,
            output_path TEXT,
            status TEXT,
            original_bytes INTEGER,
            compressed_bytes INTEGER,
            pct_saved REAL,
            text_match_ratio REAL,
            duration_seconds REAL,
            error_message TEXT,
            completed_at TEXT
        )
        """
    )
    conn.commit()
 
 
def already_done(conn, path_str):
    """Only treat as done if it previously SUCCEEDED -- a prior failure
    should be retried automatically on --resume, no manual reset needed."""
    cur = conn.execute(
        "SELECT 1 FROM compress_results WHERE path = ? AND status = 'verified_ok'",
        (path_str,),
    )
    return cur.fetchone() is not None
 
def get_excluded_filenames(conn):
    """Filenames (not full paths -- the OCR output directory has moved once
    already, so matching by directory prefix isn't reliable) whose original
    inventory row has been flagged excluded ("not a cookbook")."""
    cur = conn.execute(
        """
        SELECT o.output_path FROM ocr_results o
        JOIN inventory i ON i.path = o.path
        WHERE i.excluded = 1 AND o.output_path IS NOT NULL
        """
    )
    return {Path(row[0]).name for row in cur.fetchall() if row[0]}
 
def record_result(conn, path_str, output_path, status, orig_bytes, comp_bytes,
                   pct_saved, text_ratio, duration, err):
    conn.execute(
        """
        INSERT INTO compress_results
            (path, output_path, status, original_bytes, compressed_bytes,
             pct_saved, text_match_ratio, duration_seconds, error_message, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(path) DO UPDATE SET
            output_path=excluded.output_path, status=excluded.status,
            original_bytes=excluded.original_bytes, compressed_bytes=excluded.compressed_bytes,
            pct_saved=excluded.pct_saved, text_match_ratio=excluded.text_match_ratio,
            duration_seconds=excluded.duration_seconds,
            error_message=excluded.error_message, completed_at=excluded.completed_at
        """,
        (path_str, str(output_path), status, orig_bytes, comp_bytes,
         pct_saved, text_ratio, duration, err),
    )
    conn.commit()  # commit after every file -- never lose more than the file in flight
 
 
def extract_text(pdf_path: Path):
    if not PDFTOTEXT_AVAILABLE:
        return None, "pdftotext not installed"
    try:
        result = subprocess.run(
            ["pdftotext", str(pdf_path), "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=300, text=True,
        )
        if result.returncode != 0:
            return None, (result.stderr or "")[-500:]
        return result.stdout, None
    except subprocess.TimeoutExpired:
        return None, "pdftotext timed out"
    except Exception as e:
        return None, str(e)
 
 
def normalize_text(text: str) -> str:
    return " ".join(text.split())
 
 
def text_similarity(before: str, after: str) -> float:
    """Order-independent word-multiset comparison (Dice coefficient).
    Deliberately ignores word ORDER -- only word content/counts matter --
    since Ghostscript can shuffle pdftotext's reading-order guess without
    actually losing or changing any text. A dropped word, a garbled OCR
    substitution, or a missing paragraph all show up here as reduced
    overlap; harmless reordering does not."""
    before_words = Counter(normalize_text(before).split())
    after_words = Counter(normalize_text(after).split())
    total = sum(before_words.values()) + sum(after_words.values())
    if total == 0:
        return 1.0
    intersection = sum((before_words & after_words).values())
    return 2 * intersection / total
 
 
def repair_with_qpdf(input_path: Path, timeout: int = 300):
    """Pre-sanitize a PDF with qpdf before handing it to Ghostscript.
 
    Why: Ghostscript's own on-the-fly repair (triggered on things like
    "invalid xref entry, rebuilding xref table") is much weaker than
    qpdf's -- on some malformed files gs's rebuild silently decides the
    file has zero pages and writes out a "successful" but empty PDF
    (exit code 0, no error), rather than failing loudly. This shows up
    almost exclusively on natively-readable PDFs that never passed
    through ocrmypdf's own repair/sanitization step -- ocrmypdf's
    OCR'd output has already been normalized, so this mostly matters
    for --file-list runs against original, un-OCR'd source files
    (old scans, PDFs from varied/unknown producers, etc.).
 
    Returns (path_to_feed_gs, used_repair: bool, note: str | None).
    On any failure (qpdf missing, times out, can't fix it), falls back
    to the original input_path unchanged -- never worse than before.
    """
    if not QPDF_AVAILABLE:
        return input_path, False, None
 
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        result = subprocess.run(
            ["qpdf", str(input_path), str(tmp_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, text=True,
        )
        # qpdf exit codes: 0 = clean, 3 = warnings only (output still valid),
        # anything else = it could not produce a trustworthy repaired copy.
        if result.returncode in (0, 3) and tmp_path.exists() and tmp_path.stat().st_size > 0:
            note = None
            if result.returncode == 3:
                note = "qpdf repaired warnings: " + (result.stderr or "").strip()[-300:]
            return tmp_path, True, note
        tmp_path.unlink(missing_ok=True)
        return input_path, False, f"qpdf could not repair (exit {result.returncode}), using original as-is"
    except subprocess.TimeoutExpired:
        tmp_path.unlink(missing_ok=True)
        return input_path, False, "qpdf repair timed out, using original as-is"
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        return input_path, False, f"qpdf repair error ({e}), using original as-is"
 
 
def run_ghostscript(input_path: Path, output_path: Path, color_dpi: int,
                     gray_dpi: int, mono_dpi: int, timeout: int):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
        "-dDownsampleColorImages=true", f"-dColorImageResolution={color_dpi}",
        "-dDownsampleGrayImages=true", f"-dGrayImageResolution={gray_dpi}",
        "-dDownsampleMonoImages=true", f"-dMonoImageResolution={mono_dpi}",
        "-dNOPAUSE", "-dBATCH", "-dQUIET",
        f"-sOutputFile={output_path}", str(input_path),
    ]
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, text=True,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
 
    if result.returncode != 0:
        return False, (result.stdout or "")[-1500:]
    if not output_path.exists() or output_path.stat().st_size == 0:
        return False, "ghostscript exited 0 but produced no output"
    return True, None
 
 
def process_one(input_path: Path, output_path: Path, args, verbose=False):
    """Runs gs + verification on a single file. Returns a result dict."""
    t0 = time.time()
    orig_bytes = input_path.stat().st_size
 
    gs_input, used_repair, repair_note = repair_with_qpdf(input_path, timeout=min(args.timeout, 300))
    if verbose and repair_note:
        print(f"  qpdf: {repair_note}")
    elif verbose and used_repair:
        print("  qpdf: repaired cleanly before compression")
 
    try:
        ok, err = run_ghostscript(
            gs_input, output_path, args.color_dpi, args.gray_dpi, args.mono_dpi, args.timeout
        )
    finally:
        if used_repair and gs_input != input_path:
            gs_input.unlink(missing_ok=True)
    duration = time.time() - t0
 
    if not ok:
        return dict(status="failed", orig_bytes=orig_bytes, comp_bytes=0,
                     pct_saved=0.0, text_ratio=0.0, duration=duration, err=err)
 
    comp_bytes = output_path.stat().st_size
    pct_saved = 100.0 * (1 - comp_bytes / orig_bytes) if orig_bytes else 0.0
 
    before_text, before_err = extract_text(input_path)
    after_text, after_err = extract_text(output_path)
 
    if before_err or after_err:
        ratio = 0.0
        err = f"text extraction failed: before_err={before_err} after_err={after_err}"
        status = "compressed_unverified"
    else:
        ratio = text_similarity(before_text, after_text)
        if ratio >= TEXT_MATCH_THRESHOLD:
            status = "verified_ok"
            err = None
        else:
            status = "text_mismatch"
            err = f"text similarity only {ratio:.4f} (threshold {TEXT_MATCH_THRESHOLD})"
 
    if verbose:
        print(f"\n--- {input_path.name} ---")
        print(f"  original:   {orig_bytes/1e6:.1f} MB")
        print(f"  compressed: {comp_bytes/1e6:.1f} MB  ({pct_saved:.1f}% smaller)")
        print(f"  text match ratio: {ratio:.4f}  -> {status}")
        if before_text is not None:
            print(f"  before chars: {len(normalize_text(before_text))}")
        if after_text is not None:
            print(f"  after chars:  {len(normalize_text(after_text))}")
        if err:
            print(f"  note: {err}")
 
    return dict(status=status, orig_bytes=orig_bytes, comp_bytes=comp_bytes,
                 pct_saved=pct_saved, text_ratio=ratio, duration=duration, err=err)
 
 
def main():
    ap = argparse.ArgumentParser(description="Phase 2: shrink OCR'd PDFs, keep the text layer")
    ap.add_argument("--db", required=True, help="inventory.sqlite (shared with Phase 1.5)")
    ap.add_argument("--source", required=True, help="directory of OCR'd PDFs (Phase 1.5 output)")
    ap.add_argument("--output-dir", required=True, help="where compressed copies get written")
    ap.add_argument("--color-dpi", type=int, default=200, help="target DPI for color images (default 200)")
    ap.add_argument("--gray-dpi", type=int, default=200, help="target DPI for grayscale images (default 200)")
    ap.add_argument("--mono-dpi", type=int, default=300, help="target DPI for bitonal/mono images (default 300)")
    ap.add_argument("--timeout", type=int, default=2400, help="per-file timeout in seconds (default 2400 = 40 min)")
    ap.add_argument("--limit", type=int, default=0, help="only process the first N files")
    ap.add_argument("--resume", action="store_true", help="skip files already verified successful")
    ap.add_argument("--file", default=None,
                     help="canary mode: process ONLY this one file, print a detailed report, "
                          "and always reprocess even if already recorded")
    ap.add_argument("--file-list", default=None,
                     help="process exactly the paths listed in this text file (one absolute "
                          "path per line), instead of scanning --source with rglob. Lets you "
                          "target files that live outside --source, e.g. natively-readable "
                          "PDFs that skipped OCR entirely.")
    args = ap.parse_args()
 
    if not GS_AVAILABLE:
        print("ERROR: 'gs' (Ghostscript) not found on PATH.", file=sys.stderr)
        sys.exit(1)
    if not PDFTOTEXT_AVAILABLE:
        print("WARNING: 'pdftotext' not found -- verification will be skipped. "
              "Install poppler-utils to enable it.", file=sys.stderr)
    if not QPDF_AVAILABLE:
        print("WARNING: 'qpdf' not found -- skipping the pre-Ghostscript repair step. "
              "Some malformed PDFs (e.g. old scans with broken xref tables) can make "
              "Ghostscript silently emit an empty, 'successful' output. Install qpdf "
              "(e.g. 'sudo apt install qpdf') to catch these automatically.", file=sys.stderr)
 
    source = Path(args.source).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
 
    conn = sqlite3.connect(args.db)
    init_results_table(conn)
 
    # ---- canary mode: one file, verbose, no skip logic ----
    if args.file:
        input_path = Path(args.file).expanduser().resolve()
        if not input_path.exists():
            print(f"ERROR: file not found: {input_path}", file=sys.stderr)
            sys.exit(1)
        try:
            rel = input_path.relative_to(source)
        except ValueError:
            rel = Path(input_path.name)
        output_path = output_dir / rel
 
        print(f"CANARY TEST: {input_path}")
        print(f"  -> {output_path}")
        print(f"  targets: color={args.color_dpi}dpi gray={args.gray_dpi}dpi mono={args.mono_dpi}dpi\n")
 
        res = process_one(input_path, output_path, args, verbose=True)
        record_result(conn, str(input_path), output_path, res["status"], res["orig_bytes"],
                      res["comp_bytes"], res["pct_saved"], res["text_ratio"], res["duration"], res["err"])
 
        print(f"\nResult recorded as '{res['status']}' in compress_results.")
        if res["status"] != "verified_ok":
            print("This did NOT verify cleanly -- inspect before running the full batch.")
            sys.exit(1)
        print("Looks good. Review the output file yourself, then run the full batch with --resume.")
        conn.close()
        return
 
    # ---- full batch mode (directory scan, or an explicit --file-list) ----
    if args.file_list:
        list_path = Path(args.file_list).expanduser()
        if not list_path.exists():
            print(f"ERROR: file list not found: {list_path}", file=sys.stderr)
            sys.exit(1)
        pdf_paths = []
        for line in list_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            p = Path(line).expanduser().resolve()
            if not p.exists():
                print(f"WARNING: listed file not found, skipping: {p}", file=sys.stderr)
                continue
            pdf_paths.append(p)
        print(f"Loaded {len(pdf_paths)} file(s) from {list_path}")
    else:
        pdf_paths = sorted(source.rglob("*.pdf"))
        print(f"Found {len(pdf_paths)} PDF(s) under {source}")

    excluded_names = get_excluded_filenames(conn)
    if excluded_names:
        before = len(pdf_paths)
        pdf_paths = [p for p in pdf_paths if p.name not in excluded_names]
        skipped = before - len(pdf_paths)
        if skipped:
            print(f"Skipping {skipped} file(s) flagged excluded (not a cookbook) in inventory")

    if args.limit:
        pdf_paths = pdf_paths[: args.limit]
        print(f"--limit set: only processing first {len(pdf_paths)}") 
    stats = {"verified_ok": 0, "text_mismatch": 0, "compressed_unverified": 0, "failed": 0, "skipped": 0}
 
    for input_path in tqdm(pdf_paths, desc="Compress", unit="file"):
        path_str = str(input_path)
        if args.resume and already_done(conn, path_str):
            stats["skipped"] += 1
            continue
 
        # Files from --file-list won't generally live under --source, so
        # fall back to a flat filename (mirrors canary-mode behavior) rather
        # than raising when relative_to() can't find a common root.
        try:
            rel = input_path.relative_to(source)
        except ValueError:
            rel = Path(input_path.name)
        output_path = output_dir / rel
 
        res = process_one(input_path, output_path, args, verbose=False)
        record_result(conn, path_str, output_path, res["status"], res["orig_bytes"],
                      res["comp_bytes"], res["pct_saved"], res["text_ratio"], res["duration"], res["err"])
        stats[res["status"]] = stats.get(res["status"], 0) + 1
 
    print("\n=== Compression pass summary ===")
    for k, v in stats.items():
        print(f"  {k:24s} {v}")
    print(f"\nCompressed copies written under: {output_dir}")
    print(f"Results recorded in compress_results table in: {args.db}")
    if stats.get("text_mismatch") or stats.get("compressed_unverified"):
        print(
            "\nSome files didn't verify cleanly -- check them with:\n"
            "  SELECT path, status, text_match_ratio, error_message FROM compress_results "
            "WHERE status != 'verified_ok';"
        )
    conn.close()
 
 
if __name__ == "__main__":
    main()
 
