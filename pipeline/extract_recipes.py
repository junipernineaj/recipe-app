#!/usr/bin/env python3
"""
Phase 2: Extract structured recipes from one cookbook's OCR'd text using
the Claude API, book by book.

Why book by book: lets you watch quality and real measured cost per book
before ever committing to the full library, rather than guessing up front.

How it works:
  - Runs pdftotext against the given PDF. pdftotext inserts a form-feed
    (\f) between pages by default, so page boundaries are exact -- no
    guessing where one page ends and the next begins.
  - Groups --pages-per-chunk pages together (default 15) and sends each
    chunk to Claude with an extraction prompt.
  - Every chunk's REAL input/output token usage comes straight from the
    API's own response (response.usage), not an estimate -- so the final
    summary is a measured cost for this specific book, useful for
    projecting the cost of the rest of the library.
  - Resumable: a chunk already recorded in extraction_log is skipped on
    --resume, same convention as the rest of this pipeline.
  - Extracted recipes land in recipes.db with status='pending' -- nothing
    shows up in the live app until you review and approve it.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python3 extract_recipes.py \
        --pdf "/media/aj9/Juniper13/cookbook_ocr_compressed/Nigella Lawson - Feast.pdf" \
        --book-title "Nigella Lawson - Feast" \
        --db ~/recipe-app/recipes.db --resume

    # smoke-test on just the first 2 chunks before committing to the whole book
    python3 extract_recipes.py --pdf "..." --book-title "..." --db ... --sample 2

Known limitation: chunks don't overlap, so a recipe that straddles a chunk
boundary (e.g. starts on the last page of chunk 3, finishes on the first
page of chunk 4) may come out incomplete or get picked up twice. Recipes
land as 'pending' precisely so this kind of thing gets caught at review
time rather than silently trusted.
"""
import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable

try:
    import anthropic
except ImportError:
    print("ERROR: the 'anthropic' package isn't installed in this venv.\n"
          "Run: pip install anthropic --break-system-packages", file=sys.stderr)
    sys.exit(1)

# Pricing as of today (Sept 2026) -- check platform.claude.com/docs/en/about-claude/pricing
# before trusting this for a big run, prices do change.
PRICING_PER_MTOK = {
    "claude-sonnet-5":         {"input": 2.00, "output": 10.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
}

EXTRACTION_PROMPT = """Extract every actual recipe from the following cookbook text into a JSON array.

For each recipe, include these fields:
- "title": the recipe's name
- "servings": string, only if explicitly stated in the text (otherwise omit)
- "prep_time": string, only if explicitly stated (otherwise omit) -- never estimate one yourself
- "cook_time": string, only if explicitly stated (otherwise omit) -- never estimate one yourself
- "ingredients": a list of strings, one per ingredient, in the exact order they appear in the source text
- "instructions": a list of strings, one per method step, in the exact order they appear in the source text
- "notes": a list of strings, for any tip, aside, or sidebar note that isn't a core instruction (empty list if none)
- "flagged_for_review": a short string explaining any uncertainty (e.g. an OCR-garbled quantity you had to guess at), omit entirely if nothing is uncertain

This is a transcription task, not a rewriting task. Copy the exact wording,
phrasing, and sentence structure from the source text for every ingredient
and every instruction step -- split into list items only where the source
already breaks into separate lines or steps. Do not paraphrase, summarize,
condense multiple sentences into one, change word choice, or reorder,
regroup, or reorganize items into what seems like a more logical sequence.
If you notice yourself compressing or rewording a sentence, stop and copy
the original wording instead.

Skip anything that is not an actual recipe: front matter, copyright pages, tables of
contents, essays/introductions, reference tables (like a cooking-time chart) that have
no ingredients or method of their own, and an alphabetical index or glossary near the
back of the book that lists dish or ingredient names with page-number cross-references
instead of actual ingredients and method. A telltale sign of an index rather than real
recipes: the same name appears more than once, grouped under different headings -- e.g.
once under its own name and again under a broader category like "salads" or "weddings"
-- with no real ingredients or instructions attached to any occurrence of it.

If an ingredient quantity looks OCR-corrupted (e.g. a garbled fraction like "1/9" or
"'/z"), make your best reasonable guess at the intended value and explain the guess in
flagged_for_review -- never invent a value with no textual basis, and never leave the
field blank instead of guessing.

Do not ask clarifying questions and do not respond with anything except the JSON array.
If information is ambiguous or missing, make the most reasonable choice yourself and
note it in flagged_for_review. Output ONLY the JSON array, starting with [ and ending
with ] -- no other text before or after it. If there are no real recipes in this text,
output an empty array: []

TEXT:
{chunk_text}
"""


def pdftotext_pages(pdf_path: Path) -> list[str]:
    """Returns the PDF's text as a list of per-page strings, using pdftotext's
    own form-feed page separators -- exact page boundaries, no guessing."""
    result = subprocess.run(
        ["pdftotext", str(pdf_path), "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: pdftotext failed: {result.stderr[-500:]}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.split("\f")


def chunk_pages(pages: list[str], pages_per_chunk: int):
    """Yields (chunk_index, page_start, page_end, chunk_text) tuples.
    Pages are 1-indexed to match how a human would refer to them."""
    for i in range(0, len(pages), pages_per_chunk):
        group = pages[i:i + pages_per_chunk]
        page_start = i + 1
        page_end = i + len(group)
        yield i // pages_per_chunk, page_start, page_end, "\n\f\n".join(group)


def init_db(db_path: Path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS extraction_log (
            source_path TEXT,
            chunk_index INTEGER,
            page_start INTEGER,
            page_end INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            recipes_found INTEGER,
            completed_at TEXT,
            PRIMARY KEY (source_path, chunk_index)
        )
    """)

    # The original primary key (source_path, chunk_index) predates having more
    # than one extraction engine. Running the Claude API and a local Ollama
    # model against the same PDF would collide on chunk_index, and each
    # engine's --resume would wrongly think the OTHER engine's work was its
    # own. Migrate to a 3-column key (source_path, chunk_index, engine),
    # tagging pre-existing rows as 'claude-api' since that's the only engine
    # that existed before this column did.
    cur.execute("PRAGMA table_info(extraction_log)")
    log_cols = {row[1] for row in cur.fetchall()}
    if "engine" not in log_cols:
        cur.execute("ALTER TABLE extraction_log ADD COLUMN engine TEXT NOT NULL DEFAULT 'claude-api'")
        cur.execute("""
            CREATE TABLE extraction_log_new (
                source_path TEXT,
                chunk_index INTEGER,
                engine TEXT NOT NULL DEFAULT 'claude-api',
                page_start INTEGER,
                page_end INTEGER,
                input_tokens INTEGER,
                output_tokens INTEGER,
                recipes_found INTEGER,
                completed_at TEXT,
                PRIMARY KEY (source_path, chunk_index, engine)
            )
        """)
        cur.execute("""
            INSERT INTO extraction_log_new
                (source_path, chunk_index, engine, page_start, page_end,
                 input_tokens, output_tokens, recipes_found, completed_at)
            SELECT source_path, chunk_index, engine, page_start, page_end,
                   input_tokens, output_tokens, recipes_found, completed_at
            FROM extraction_log
        """)
        cur.execute("DROP TABLE extraction_log")
        cur.execute("ALTER TABLE extraction_log_new RENAME TO extraction_log")
        print("Migrated extraction_log: added 'engine' to the primary key (existing rows tagged 'claude-api')")

    # Add the new columns to the existing recipes table if they're not there yet.
    cur.execute("PRAGMA table_info(recipes)")
    existing_cols = {row[1] for row in cur.fetchall()}
    new_cols = {
        "source_path": "TEXT",
        "source_page": "INTEGER",
        "servings": "TEXT",
        "prep_time": "TEXT",
        "cook_time": "TEXT",
        "notes": "TEXT",
        "flagged_for_review": "TEXT",
        "status": "TEXT DEFAULT 'pending'",
        "source_page_end": "INTEGER",
        "extracted_at": "TEXT",
        "engine": "TEXT DEFAULT 'claude-api'",
    }
    for col, col_type in new_cols.items():
        if col not in existing_cols:
            cur.execute(f"ALTER TABLE recipes ADD COLUMN {col} {col_type}")
            print(f"Migrated recipes table: added column '{col}'")

    conn.commit()
    return conn


def already_done(conn, source_path: str, chunk_index: int, engine: str = "claude-api") -> bool:
    cur = conn.execute(
        "SELECT 1 FROM extraction_log WHERE source_path = ? AND chunk_index = ? AND engine = ?",
        (source_path, chunk_index, engine),
    )
    return cur.fetchone() is not None


def call_claude(client, model, chunk_text, retries=2):
    """Calls the API and parses the JSON array out of the response. Retries
    with a corrective follow-up if the model doesn't return clean JSON, or
    with a larger token budget if the response got cut off mid-output --
    cheap insurance against the odd malformed or truncated response."""
    prompt = EXTRACTION_PROMPT.format(chunk_text=chunk_text)
    last_error = None
    max_tokens = 16000
    total_in_tok = 0
    total_out_tok = 0
    for attempt in range(retries + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                # Sonnet 5 runs adaptive thinking by default on any call that
                # doesn't set this -- unlike 4.6, where omitting it meant no
                # thinking. Thinking tokens bill as output tokens and eat into
                # max_tokens, which was the real cause of the truncated-JSON
                # failures earlier. This is plain extraction, not reasoning,
                # so we don't need it: turning it off cuts cost and gives the
                # full max_tokens budget to the actual JSON.
                thinking={"type": "disabled"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            last_error = f"API call failed: {e}"
            time.sleep(2 ** attempt)
            continue

        # Every attempt costs real tokens even if we end up discarding the
        # response, so tally usage here rather than only on the final return --
        # otherwise a chunk that fails after 3 paid attempts gets logged as free.
        total_in_tok += response.usage.input_tokens
        total_out_tok += response.usage.output_tokens

        # Newer models can return a leading ThinkingBlock alongside the actual
        # TextBlock -- content[0] isn't reliably the text, so pull out every
        # text block instead of assuming position.
        text_blocks = [block.text for block in response.content if block.type == "text"]
        if not text_blocks:
            last_error = "response had no text block (only thinking/other content)"
            time.sleep(2 ** attempt)
            continue
        raw = "".join(text_blocks).strip()
        # Models sometimes wrap JSON in a ```json fence despite instructions -- strip it.
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())

        if response.stop_reason == "max_tokens":
            # Cut off mid-output -- a "that wasn't valid JSON" follow-up won't
            # help here, it'll just get truncated again at the same budget.
            # Double the budget instead and resend the original prompt.
            last_error = f"response was truncated at {max_tokens} output tokens"
            max_tokens = min(max_tokens * 2, 32000)
            continue

        try:
            recipes = json.loads(raw)
            if not isinstance(recipes, list):
                raise ValueError("response was valid JSON but not a list")
            return recipes, total_in_tok, total_out_tok
        except (json.JSONDecodeError, ValueError) as e:
            last_error = f"could not parse JSON: {e}"
            prompt = (EXTRACTION_PROMPT.format(chunk_text=chunk_text) +
                      f"\n\nYour previous response was not valid JSON ({e}). "
                      f"Output ONLY the JSON array this time, nothing else.")

    print(f"WARNING: giving up on this chunk after {retries + 1} attempts -- {last_error}",
          file=sys.stderr)
    return [], total_in_tok, total_out_tok


def looks_like_index_chunk(recipes: list) -> bool:
    """Deterministic backstop for a failure mode the extraction prompt's
    "skip alphabetical indexes/glossaries" instruction doesn't reliably
    catch on its own -- confirmed in practice on 2026-09-24, where
    qwen3:14b returned 147 "recipes" from a 15-page chunk that was purely
    a back-of-book index, despite that exact instruction being right there
    in the prompt. A chunk that returns an unusually large number of
    recipe-shaped things, almost all of which have no real ingredients or
    instructions, is far more likely to be an index/glossary/reference
    list than that many genuinely complete recipes crammed into one
    chunk -- so treat it as one and discard the whole batch, rather than
    inserting dozens of content-free stubs a human would then have to
    reject one at a time in the review queue."""
    if len(recipes) < 8:
        return False
    blank_count = sum(
        1 for r in recipes
        if not [l for l in r.get("ingredients", []) if l and l.strip()]
        or not [l for l in r.get("instructions", []) if l and l.strip()]
    )
    return (blank_count / len(recipes)) >= 0.6


def insert_recipe(conn, recipe, book_title, source_path, page_start, page_end, engine="claude-api"):
    # The JSON schema only enforces that ingredients/instructions are
    # present, not that they contain anything real -- a model can satisfy
    # it with a blank/whitespace-only entry (seen in practice: a recipe
    # with a real title but an empty ingredients list and an empty
    # instructions list). Strip blanks and, if either list ends up empty,
    # flag it loudly rather than silently inserting something that looks
    # like a normal reviewable recipe until you click in and find nothing,
    # or silently dropping it and losing the fact that a recipe exists on
    # that page at all.
    ingredients = [line for line in recipe.get("ingredients", []) if line and line.strip()]
    instructions = [line for line in recipe.get("instructions", []) if line and line.strip()]

    flagged_for_review = recipe.get("flagged_for_review")
    # Some models answer "is anything wrong? no" by writing the literal
    # string "false" into this field instead of just omitting it like the
    # prompt asks (seen in practice with qwen3:14b). That's still a
    # non-empty string, so it reads as truthy everywhere the app checks
    # `if recipe["flagged_for_review"]`, giving the recipe an unwarranted
    # "flagged" banner in the review queue. Treat it the same as no flag.
    if isinstance(flagged_for_review, str) and flagged_for_review.strip().lower() == "false":
        flagged_for_review = None
    if not ingredients or not instructions:
        missing = " or ".join(
            name for name, values in (("ingredients", ingredients), ("instructions", instructions))
            if not values
        )
        note = f"Extraction returned no {missing} for this recipe -- check the source page and fill in by hand or reject."
        flagged_for_review = f"{flagged_for_review} {note}" if flagged_for_review else note

    conn.execute("""
        INSERT INTO recipes
            (title, source_book, source_path, source_page, source_page_end,
             ingredients, instructions, servings, prep_time, cook_time,
             notes, flagged_for_review, status, extracted_at, engine)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', datetime('now'), ?)
    """, (
        recipe.get("title", "(untitled)"),
        book_title,
        source_path,
        page_start,
        page_end,
        "\n".join(ingredients),
        "\n".join(instructions),
        recipe.get("servings"),
        recipe.get("prep_time"),
        recipe.get("cook_time"),
        "\n".join(recipe.get("notes", [])) if recipe.get("notes") else None,
        flagged_for_review,
        engine,
    ))


def main():
    ap = argparse.ArgumentParser(description="Phase 2: extract recipes from one cookbook via Claude API")
    ap.add_argument("--pdf", required=True, help="path to the book's PDF (the compressed copy is fine)")
    ap.add_argument("--book-title", required=True, help="display name to store as source_book")
    ap.add_argument("--db", required=True, help="recipes.db to write into")
    ap.add_argument("--pages-per-chunk", type=int, default=15)
    ap.add_argument("--model", default="claude-sonnet-5", choices=list(PRICING_PER_MTOK))
    ap.add_argument("--resume", action="store_true", help="skip chunks already recorded in extraction_log")
    ap.add_argument("--sample", type=int, default=0, help="only process the first N chunks (smoke test)")
    args = ap.parse_args()

    pdf_path = Path(args.pdf).expanduser()
    if not pdf_path.exists():
        print(f"ERROR: file not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    conn = init_db(Path(args.db).expanduser())
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment

    print(f"Extracting text from {pdf_path.name} ...")
    pages = pdftotext_pages(pdf_path)
    print(f"{len(pages)} page(s) found.")

    chunks = list(chunk_pages(pages, args.pages_per_chunk))
    if args.sample:
        chunks = chunks[:args.sample]
        print(f"--sample set: only processing the first {len(chunks)} chunk(s)")

    total_input_tokens = 0
    total_output_tokens = 0
    total_recipes = 0
    source_path = str(pdf_path)

    for chunk_index, page_start, page_end, chunk_text in tqdm(chunks, desc="Extract", unit="chunk"):
        if args.resume and already_done(conn, source_path, chunk_index):
            continue

        recipes, in_tok, out_tok = call_claude(client, args.model, chunk_text)
        if looks_like_index_chunk(recipes):
            print(f"NOTE: chunk {chunk_index} (pages {page_start}-{page_end}) looks like an "
                  f"index/glossary ({len(recipes)} items, mostly blank) -- skipping rather "
                  f"than inserting as recipes", file=sys.stderr)
            recipes = []
        for recipe in recipes:
            insert_recipe(conn, recipe, args.book_title, source_path, page_start, page_end)

        conn.execute("""
            INSERT INTO extraction_log
                (source_path, chunk_index, engine, page_start, page_end,
                 input_tokens, output_tokens, recipes_found, completed_at)
            VALUES (?, ?, 'claude-api', ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(source_path, chunk_index, engine) DO UPDATE SET
                input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,
                recipes_found=excluded.recipes_found, completed_at=excluded.completed_at
        """, (source_path, chunk_index, page_start, page_end, in_tok, out_tok, len(recipes)))
        conn.commit()  # commit after every chunk -- never lose more than the chunk in flight

        total_input_tokens += in_tok
        total_output_tokens += out_tok
        total_recipes += len(recipes)

    pricing = PRICING_PER_MTOK[args.model]
    cost = (total_input_tokens / 1_000_000 * pricing["input"] +
            total_output_tokens / 1_000_000 * pricing["output"])

    print("\n=== Extraction summary ===")
    print(f"  Book:              {args.book_title}")
    print(f"  Chunks processed:  {len(chunks)}")
    print(f"  Recipes found:     {total_recipes}")
    print(f"  Input tokens:      {total_input_tokens:,}")
    print(f"  Output tokens:     {total_output_tokens:,}")
    print(f"  Measured cost:     ${cost:.4f}  (model: {args.model}, live API rate)")
    print(f"  Recipes saved with status='pending' -- review before they show up live.")
    conn.close()


if __name__ == "__main__":
    main()
