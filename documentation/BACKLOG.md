# Backlog

Ideas that have been discussed but deliberately parked rather than built —
either because the shape of the thing isn't settled yet, or because it's
just not the priority right now. Each entry captures what was actually
said (Tony's framing, in his own terms) plus whatever came out of
thinking it through, so picking it back up later doesn't mean starting
the discussion from scratch.

## Non-PDF recipe sources (Instagram, Facebook, Claude suggestions)

Raised 2026-09-27. Tony wants a way to add recipes that don't come from a
PDF cookbook — an Instagram or Facebook post, or something Claude
suggested in conversation (his example: the saag aloo and tofu curry made
that day) — rather than requiring every recipe to trace back to a scanned
book page. His framing: these sources won't have a PDF, just a link to
something on the web, and that link will expire over time — whatever the
design is needs to survive that.

**What's already there to build on:** `main.py` has a bare-bones
manual-add path (`GET`/`POST /recipes/new` → `POST /recipes` →
`create_recipe()`) that already bypasses the whole PDF pipeline — title,
`source_book`, ingredients, instructions typed in directly, no
`source_path`/`source_page` required. That's the natural starting point,
not something built from nothing.

**Observations from thinking it through:**

- The recipe's actual content (ingredients, instructions, etc.) is
  already stored as plain text, independent of where it came from — that
  part needs no schema change at all.
- `source_book` / `source_path` / `source_page` are all PDF-book-shaped
  concepts (the latter two point at a specific page in a specific scanned
  file); a URL doesn't fit any of them cleanly.
- Proposed shape (not decided): a `source_type` column (`book` / `web` /
  `personal`, say) plus an optional `source_url` and a free-text note,
  rather than overloading `source_book`'s "Author - Title" convention
  with things like "@chefname on Instagram."
- **On link rot:** treat the URL as a courtesy citation, never the source
  of truth — the durable copy is the ingredients/instructions text
  captured at add-time, the same way the PDF pipeline never needs to
  re-read the original file once text has been extracted from it.
- `/authors` currently assumes every recipe belongs to a book with an
  author; web/personal recipes don't fit that grouping and would need
  either their own section there, or to just live outside `/authors`
  entirely (still fully browsable/searchable from the homepage as normal).
- Auto-fetching content from a pasted URL was raised as an option, but
  flagged as unreliable for Instagram/Facebook specifically — both sit
  behind logins and actively block this kind of automated access, so
  manual paste/type-in is the only genuinely reliable path for those two.
  It could plausibly work for an ordinary recipe-blog URL, if that's ever
  wanted.
- **Found in passing, not yet fixed:** the existing manual-add route
  (`create_recipe()`) never sets an initial `status` on the row it
  inserts. Because SQL's `NULL != 'approved'` is neither true nor false,
  a manually-added recipe currently lands in limbo — not `approved` (so
  it doesn't show on the site) but also invisible to the review queue's
  `WHERE status != 'approved'` filter. Worth fixing (just needs an
  explicit `status = 'pending'` on insert) whenever this area gets
  touched again, regardless of what else changes here.

**Status:** deliberately parked — wants more time to think through the
shape of this before deciding. Three questions were raised and left open:

1. How hands-on should adding one of these be — fully manual entry, or
   manual plus a best-effort auto-fill attempt for plain recipe-blog
   URLs (not Instagram/Facebook, which would still need manual entry
   either way)?
2. Should these show up on `/authors` alongside PDF cookbooks (e.g. an
   "Other sources" section), or stay off it entirely and just be
   reachable via the homepage/search?
3. How much detail to capture about provenance — just a source type +
   optional link + short note, or also a dedicated field to paste the
   original caption/description verbatim as a permanent local snapshot?

Come back to this when ready to pick a direction.
