#!/usr/bin/env bash
#
# weekly_refresh.sh — run the full cookbook pipeline over whatever's new.
#
# Safe to re-run any time: every stage is --resume-aware and skips files
# it has already handled successfully. Drop new books into SOURCE_DIR,
# then just run this script.
#
# Stages, in order:
#   1. cookbook_inventory.py --resume  -> classifies any new files
#   2. ocr_pass.py --resume            -> OCRs anything inventory marked needs_ocr
#   3. compress_pass.py --resume       -> compresses anything newly OCR'd
#
# A timestamped summary is appended to weekly_refresh.log at the end.
 
set -uo pipefail
# Deliberately NOT -e: if one stage fails we still want to reach the
# summary block below rather than dying silently mid-run.
 
# ---- CONFIG: check these against your actual paths before first run --------
PROJECT_DIR="$HOME/cookbook-project"
VENV_DIR="$PROJECT_DIR/venv"                       # <-- CHECK: your venv location
DB="$PROJECT_DIR/inventory.sqlite"
SOURCE_DIR="/media/aj9/Juniper13/CHANGE_ME"         # <-- CHECK: raw library root
OCR_OUTPUT_DIR="/media/aj9/Juniper13/CHANGE_ME"     # <-- CHECK: where ocr_pass.py writes
COMPRESSED_OUTPUT_DIR="/media/aj9/Juniper13/CHANGE_ME"  # <-- CHECK: where compress_pass.py writes
LANG_CODE="eng"
JOBS=6
OCR_TIMEOUT=2400
COMPRESS_TIMEOUT=2400
COLOR_DPI=200
GRAY_DPI=200
MONO_DPI=300
LOG_FILE="$PROJECT_DIR/weekly_refresh.log"
# -----------------------------------------------------------------------------
 
cd "$PROJECT_DIR" || { echo "Can't cd to $PROJECT_DIR" >&2; exit 1; }
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
 
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') | $*"; }
 
RUN_START=$(date '+%Y-%m-%d %H:%M:%S')
RUN_START_EPOCH=$(date +%s)
 
{
  echo "============================================================="
  echo "Weekly refresh started: $RUN_START"
} | tee -a "$LOG_FILE"
 
# ---- Stage 1: inventory scan (pick up new books) ----------------------------
log "Stage 1/3: scanning for new books..." | tee -a "$LOG_FILE"
python3 cookbook_inventory.py --source "$SOURCE_DIR" --db "$DB" --resume \
    2>&1 | tee -a "$LOG_FILE"
STAGE1_STATUS=$?
 
# ---- Stage 2: OCR pass -------------------------------------------------------
log "Stage 2/3: OCR pass (--resume)..." | tee -a "$LOG_FILE"
python3 ocr_pass.py --db "$DB" --source "$SOURCE_DIR" --output-dir "$OCR_OUTPUT_DIR" \
    --lang "$LANG_CODE" --jobs "$JOBS" --timeout "$OCR_TIMEOUT" --resume \
    2>&1 | tee -a "$LOG_FILE"
STAGE2_STATUS=$?
 
# ---- Stage 3: compress pass ---------------------------------------------------
log "Stage 3/3: compress pass (--resume)..." | tee -a "$LOG_FILE"
python3 compress_pass.py --db "$DB" --source "$OCR_OUTPUT_DIR" --output-dir "$COMPRESSED_OUTPUT_DIR" \
    --color-dpi "$COLOR_DPI" --gray-dpi "$GRAY_DPI" --mono-dpi "$MONO_DPI" \
    --timeout "$COMPRESS_TIMEOUT" --resume \
    2>&1 | tee -a "$LOG_FILE"
STAGE3_STATUS=$?
 
# ---- Summary ------------------------------------------------------------------
RUN_END=$(date '+%Y-%m-%d %H:%M:%S')
ELAPSED=$(( ($(date +%s) - RUN_START_EPOCH) / 60 ))
 
INV_TOTAL=$(sqlite3 "$DB" "SELECT COUNT(*) FROM inventory;" 2>/dev/null || echo "?")
INV_ERRORS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM inventory WHERE status='error';" 2>/dev/null || echo "?")
NEEDS_OCR_PENDING=$(sqlite3 "$DB" "SELECT COUNT(*) FROM inventory WHERE status='needs_ocr';" 2>/dev/null || echo "?")
OCR_OK=$(sqlite3 "$DB" "SELECT COUNT(*) FROM ocr_results WHERE ocr_status LIKE 'ocr_success%';" 2>/dev/null || echo "?")
OCR_FAILED=$(sqlite3 "$DB" "SELECT COUNT(*) FROM ocr_results WHERE ocr_status='failed';" 2>/dev/null || echo "?")
COMP_OK=$(sqlite3 "$DB" "SELECT COUNT(*) FROM compress_results WHERE status='verified_ok';" 2>/dev/null || echo "?")
COMP_MISMATCH=$(sqlite3 "$DB" "SELECT COUNT(*) FROM compress_results WHERE status='text_mismatch';" 2>/dev/null || echo "?")
 
NEEDS_ATTENTION=0
[ "$STAGE1_STATUS" != "0" ] && NEEDS_ATTENTION=1
[ "$STAGE2_STATUS" != "0" ] && NEEDS_ATTENTION=1
[ "$STAGE3_STATUS" != "0" ] && NEEDS_ATTENTION=1
[ "$OCR_FAILED" != "0" ] && [ "$OCR_FAILED" != "?" ] && NEEDS_ATTENTION=1
[ "$COMP_MISMATCH" != "0" ] && [ "$COMP_MISMATCH" != "?" ] && NEEDS_ATTENTION=1
[ "$INV_ERRORS" != "0" ] && [ "$INV_ERRORS" != "?" ] && NEEDS_ATTENTION=1
 
{
  echo ""
  echo "----------------- Run summary: $RUN_END -----------------"
  echo "Duration: ${ELAPSED} min"
  echo "Exit codes   -> inventory:$STAGE1_STATUS  ocr:$STAGE2_STATUS  compress:$STAGE3_STATUS"
  echo "Inventory    -> total tracked: $INV_TOTAL, scan errors: $INV_ERRORS, still needs_ocr: $NEEDS_OCR_PENDING"
  echo "OCR results  -> success: $OCR_OK, failed: $OCR_FAILED"
  echo "Compress     -> verified_ok: $COMP_OK, text_mismatch: $COMP_MISMATCH"
  if [ "$NEEDS_ATTENTION" -eq 1 ]; then
    echo "STATUS: ATTENTION NEEDED - check the log above for details"
  else
    echo "STATUS: ALL CLEAR"
  fi
  echo "============================================================="
} | tee -a "$LOG_FILE"
 
exit "$NEEDS_ATTENTION"
 
