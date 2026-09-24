#!/usr/bin/env python3
"""
Phase 2 (local variant): Extract recipes from a cookbook using a local
Ollama model instead of the Claude API.

Same schema, same recipes.db, same resumable per-chunk design as
extract_recipes.py -- in fact it imports the shared pieces (the PDF/chunking
logic, the EXTRACTION_PROMPT, the DB helpers) straight from that file, so
both engines are guaranteed to be working from the exact same prompt and
writing to the exact same schema. That's what makes a side-by-side
comparison between them meaningful rather than apples-to-oranges.

Cost here is $0.00 -- the real constraint is your GPU's time, not your
wallet, so this reports elapsed time per chunk instead of a dollar amount,
to help judge whether running the whole library locally is actually
practical or just very slow.

Requires Ollama running and reachable (defaults to http://localhost:11434,
i.e. running this ON junipernine2 itself), with the model already pulled:
    ollama pull qwen2.5:14b

Usage:
    python3 extract_recipes_local.py \
        --pdf "/media/aj9/Juniper13/cookbook_ocr_compressed/Nigella Lawson - Feast.pdf" \
        --book-title "Nigella Lawson - Feast" \
        --db ~/recipe-app/recipes.db \
        --model qwen2.5:14b \
        --sample 2

Known limitation: same chunk-boundary caveat as extract_recipes.py --
recipes straddling a chunk boundary may come out incomplete or duplicated.
Also worth knowing: a smaller local model is more likely to miss subtle
OCR-garbling judgment calls (or fail to flag them) than Sonnet was in
testing -- that's exactly what the 'engine' column on both tables is for,
so you can filter review by which engine produced a given recipe.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import requests

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable

from extract_recipes import (
    EXTRACTION_PROMPT,
    already_done,
    chunk_pages,
    init_db,
    insert_recipe,
    pdftotext_pages,
)

# Ollama's structured-outputs feature: passing a JSON schema as `format`
# constrains generation so the model can't return malformed JSON. We wrap
# the recipe array in an object (rather than making the array the schema
# root) because grammar-constrained decoding tends to be more reliable
# starting from an object -- the shared prompt still tells the model to
# output a bare array, but the schema is what actually governs the shape
# here, so we just unwrap "recipes" after parsing.
RECIPE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "recipes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "servings": {"type": "string"},
                    "prep_time": {"type": "string"},
                    "cook_time": {"type": "string"},
                    "ingredients": {"type": "array", "items": {"type": "string"}},
                    "instructions": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "array", "items": {"type": "string"}},
                    "flagged_for_review": {"type": "string"},
                },
                "required": ["title", "ingredients", "instructions"],
            },
        }
    },
    "required": ["recipes"],
}


def call_ollama(host, model, chunk_text, retries=2):
    """Calls a local Ollama server and parses the recipe array out of the
    response. Retries with a corrective follow-up if the model doesn't
    return something matching the schema -- the schema constrains syntax,
    not content, so a model can still hallucinate or wander off it."""
    prompt = EXTRACTION_PROMPT.format(chunk_text=chunk_text)
    last_error = None
    total_prompt_tok = 0
    total_eval_tok = 0
    for attempt in range(retries + 1):
        try:
            response = requests.post(
                f"{host}/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "format": RECIPE_RESPONSE_SCHEMA,
                    "options": {
                        "temperature": 0.2,
                        # Without a cap, a model can spiral into repeating
                        # itself indefinitely instead of emitting the JSON
                        # array's closing bracket -- seen in practice on
                        # 2026-09-24 (a single chunk ran to 23,000+ generated
                        # tokens and overflowed the context window before the
                        # 600s request timeout finally cut it off). num_predict
                        # forces a much cheaper failure well before that, and
                        # repeat_penalty makes the underlying loop less likely
                        # to start in the first place. A run that actually
                        # needs more than this many tokens for one chunk was
                        # going to produce something too large to trust anyway.
                        "num_predict": 8000,
                        "repeat_penalty": 1.15,
                    },
                },
                timeout=600,
            )
            response.raise_for_status()
        except requests.RequestException as e:
            last_error = f"Ollama request failed: {e}"
            time.sleep(2 ** attempt)
            continue

        data = response.json()
        # Ollama's closest equivalent to the Anthropic API's usage.input/output
        # tokens -- not billed, but useful for comparing relative chunk cost
        # and for sizing how long the rest of the library would take.
        total_prompt_tok += data.get("prompt_eval_count", 0) or 0
        total_eval_tok += data.get("eval_count", 0) or 0

        raw = (data.get("message") or {}).get("content", "").strip()
        if not raw:
            last_error = "empty response from Ollama"
            time.sleep(2 ** attempt)
            continue

        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                recipes = parsed.get("recipes", [])
            elif isinstance(parsed, list):
                # Some models ignore the object wrapper despite the schema --
                # a bare array is still fine, just take it as-is.
                recipes = parsed
            else:
                raise ValueError("response was valid JSON but not an object or list")
            if not isinstance(recipes, list):
                raise ValueError("'recipes' was present but not a list")
            return recipes, total_prompt_tok, total_eval_tok
        except (json.JSONDecodeError, ValueError) as e:
            last_error = f"could not parse JSON: {e}"
            prompt = (EXTRACTION_PROMPT.format(chunk_text=chunk_text) +
                      f"\n\nYour previous response was not valid ({e}). "
                      f"Output ONLY the JSON this time, nothing else.")

    print(f"WARNING: giving up on this chunk after {retries + 1} attempts -- {last_error}",
          file=sys.stderr)
    return [], total_prompt_tok, total_eval_tok


def main():
    ap = argparse.ArgumentParser(description="Phase 2 (local): extract recipes from one cookbook via a local Ollama model")
    ap.add_argument("--pdf", required=True, help="path to the book's PDF (the compressed copy is fine)")
    ap.add_argument("--book-title", required=True, help="display name to store as source_book")
    ap.add_argument("--db", required=True, help="recipes.db to write into")
    ap.add_argument("--pages-per-chunk", type=int, default=15)
    ap.add_argument("--model", default="qwen2.5:14b", help="Ollama model tag (must already be pulled)")
    ap.add_argument("--ollama-host", default="http://localhost:11434")
    ap.add_argument("--resume", action="store_true", help="skip chunks already recorded in extraction_log for this engine")
    ap.add_argument("--sample", type=int, default=0, help="only process the first N chunks (smoke test)")
    args = ap.parse_args()

    pdf_path = Path(args.pdf).expanduser()
    if not pdf_path.exists():
        print(f"ERROR: file not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    engine = f"ollama:{args.model}"
    conn = init_db(Path(args.db).expanduser())

    print(f"Extracting text from {pdf_path.name} ...")
    pages = pdftotext_pages(pdf_path)
    print(f"{len(pages)} page(s) found.")

    chunks = list(chunk_pages(pages, args.pages_per_chunk))
    if args.sample:
        chunks = chunks[:args.sample]
        print(f"--sample set: only processing the first {len(chunks)} chunk(s)")

    total_prompt_tok = 0
    total_eval_tok = 0
    total_recipes = 0
    source_path = str(pdf_path)
    started_at = time.monotonic()

    for chunk_index, page_start, page_end, chunk_text in tqdm(chunks, desc=f"Extract ({args.model})", unit="chunk"):
        if args.resume and already_done(conn, source_path, chunk_index, engine=engine):
            continue

        recipes, prompt_tok, eval_tok = call_ollama(args.ollama_host, args.model, chunk_text)
        for recipe in recipes:
            insert_recipe(conn, recipe, args.book_title, source_path, page_start, page_end, engine=engine)

        conn.execute("""
            INSERT INTO extraction_log
                (source_path, chunk_index, engine, page_start, page_end,
                 input_tokens, output_tokens, recipes_found, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(source_path, chunk_index, engine) DO UPDATE SET
                input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,
                recipes_found=excluded.recipes_found, completed_at=excluded.completed_at
        """, (source_path, chunk_index, engine, page_start, page_end, prompt_tok, eval_tok, len(recipes)))
        conn.commit()  # commit after every chunk -- never lose more than the chunk in flight

        total_prompt_tok += prompt_tok
        total_eval_tok += eval_tok
        total_recipes += len(recipes)

    elapsed = time.monotonic() - started_at
    per_chunk = elapsed / len(chunks) if chunks else 0
    # Rough sense of what running your whole ~700-book library at this rate
    # would take -- purely illustrative, actual page counts vary a lot.
    est_full_book_minutes = (per_chunk * len(list(chunk_pages(pages, args.pages_per_chunk)))) / 60

    print("\n=== Local extraction summary ===")
    print(f"  Book:              {args.book_title}")
    print(f"  Engine:            {engine} (Ollama at {args.ollama_host})")
    print(f"  Chunks processed:  {len(chunks)}")
    print(f"  Recipes found:     {total_recipes}")
    print(f"  Prompt tokens:     {total_prompt_tok:,}  (Ollama's prompt_eval_count -- informational, not billed)")
    print(f"  Output tokens:     {total_eval_tok:,}  (Ollama's eval_count -- informational, not billed)")
    print(f"  Elapsed:           {elapsed:.1f}s total, {per_chunk:.1f}s/chunk average")
    print(f"  Est. full book:    ~{est_full_book_minutes:.1f} min at this page count, this rate")
    print(f"  Cost:              $0.00 (local inference -- electricity only)")
    print(f"  Recipes saved with status='pending' -- review before they show up live.")
    conn.close()


if __name__ == "__main__":
    main()
