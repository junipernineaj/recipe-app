import os
import sqlite3
from fastapi import FastAPI, Request, Form, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, FileResponse

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

INVENTORY_DB_PATH = os.path.expanduser("~/cookbook-project/inventory.sqlite")


def get_recipes(search_term: str = ""):
    conn = sqlite3.connect("recipes.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if search_term:
        cursor.execute(
            "SELECT id, title, source_book FROM recipes WHERE status = 'approved' AND title LIKE ?",
            (f"%{search_term}%",)
        )
    else:
        cursor.execute("SELECT id, title, source_book FROM recipes WHERE status = 'approved'")
    recipes = cursor.fetchall()
    conn.close()
    return recipes


def get_recipe_by_id(recipe_id: int):
    conn = sqlite3.connect("recipes.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,))
    recipe = cursor.fetchone()
    conn.close()
    return recipe


def get_pending_recipes():
    conn = sqlite3.connect("recipes.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, title, source_book, engine, flagged_for_review
        FROM recipes
        WHERE status != 'approved'
        ORDER BY source_book, id
    """)
    recipes = cursor.fetchall()
    conn.close()
    return recipes


def create_recipe(title, source_book, ingredients, instructions):
    conn = sqlite3.connect("recipes.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO recipes (title, source_book, ingredients, instructions) VALUES (?, ?, ?, ?)",
        (title, source_book, ingredients, instructions)
    )
    conn.commit()
    conn.close()

def get_books(search_term: str = "", status_filter: str = "", sort: str = "filename_asc", show_hidden: bool = False):
    conn = sqlite3.connect(INVENTORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    base_query = """
        SELECT * FROM (
            SELECT
                i.path, i.filename, i.excluded,
                CASE
                    WHEN c.status = 'verified_ok' THEN c.compressed_bytes
                    ELSE i.size_bytes
                END AS size_bytes,
                MAX(
                    COALESCE(i.scanned_at, ''),
                    COALESCE(o.completed_at, ''),
                    COALESCE(c.completed_at, '')
                ) AS last_updated,
                CASE
                    WHEN i.status = 'unsupported_extension' THEN 'unsupported_extension'
                    WHEN c.status = 'verified_ok' THEN 'compressed'
                    WHEN o.ocr_status LIKE 'ocr_success%' THEN 'ocr_done'
                    WHEN i.status = 'native_force_reocred' THEN 'ocr_done'
                    WHEN i.status = 'readable_native' THEN 'readable_native'
                    ELSE 'needs_ocr'
                END AS status
            FROM inventory i
            LEFT JOIN ocr_results o ON i.path = o.path
            LEFT JOIN compress_results c ON c.path = COALESCE(o.output_path, i.path)
        )
        WHERE 1=1
    """
    params = []
    if not show_hidden:
        base_query += " AND excluded = 0"
    if status_filter:
        base_query += " AND status = ?"
        params.append(status_filter)
    else:
        base_query += " AND status != 'unsupported_extension'"
    if search_term:
        base_query += " AND filename LIKE ?"
        params.append(f"%{search_term}%")

    sort_columns = {
        "filename_asc": "filename ASC",
        "filename_desc": "filename DESC",
        "status_asc": "status ASC",
        "status_desc": "status DESC",
        "size_asc": "size_bytes ASC",
        "size_desc": "size_bytes DESC",
        "scanned_at_asc": "last_updated ASC",
        "scanned_at_desc": "last_updated DESC",
    }
    base_query += " ORDER BY " + sort_columns.get(sort, "filename ASC")

    cursor.execute(base_query, params)
    books = cursor.fetchall()
    conn.close()
    return books

def get_book_status_counts(show_hidden: bool = False):
    conn = sqlite3.connect(INVENTORY_DB_PATH)
    cursor = conn.cursor()
    query = """
        SELECT status, COUNT(*) FROM (
            SELECT
                i.excluded,
                CASE
                    WHEN i.status = 'unsupported_extension' THEN 'unsupported_extension'
                    WHEN c.status = 'verified_ok' THEN 'compressed'
                    WHEN o.ocr_status LIKE 'ocr_success%' THEN 'ocr_done'
                    WHEN i.status = 'native_force_reocred' THEN 'ocr_done'
                    WHEN i.status = 'readable_native' THEN 'readable_native'
                    ELSE 'needs_ocr'
                END AS status
            FROM inventory i
            LEFT JOIN ocr_results o ON i.path = o.path
            LEFT JOIN compress_results c ON c.path = COALESCE(o.output_path, i.path)
        )
    """
    if not show_hidden:
        query += " WHERE excluded = 0"
    query += " GROUP BY status ORDER BY COUNT(*) DESC"
    cursor.execute(query)
    counts = cursor.fetchall()
    conn.close()
    return counts

@app.get("/")
def read_root(request: Request):
    recipes = get_recipes()
    return templates.TemplateResponse(
        request=request, name="home.html", context={"recipes": recipes}
    )


@app.get("/search")
def search(request: Request, q: str = ""):
    recipes = get_recipes(q)
    return templates.TemplateResponse(
        request=request, name="recipe_list.html", context={"recipes": recipes}
    )

@app.get("/review")
def review_page(request: Request):
    recipes = get_pending_recipes()
    return templates.TemplateResponse(
        request=request, name="review.html", context={"recipes": recipes}
    )

@app.get("/books")
def books_page(request: Request, sort: str = "filename_asc", show_hidden: bool = False):
    books = get_books(sort=sort, show_hidden=show_hidden)
    counts = get_book_status_counts(show_hidden=show_hidden)
    return templates.TemplateResponse(
        request=request, name="books.html",
        context={"books": books, "counts": counts, "sort": sort, "show_hidden": show_hidden}
    )

@app.get("/books/search")
def books_search(request: Request, q: str = "", status: str = "", sort: str = "filename_asc", show_hidden: bool = False):
    books = get_books(search_term=q, status_filter=status, sort=sort, show_hidden=show_hidden)
    return templates.TemplateResponse(
        request=request, name="books_list.html",
        context={"books": books, "sort": sort, "show_hidden": show_hidden}
    )

@app.post("/books/exclude")
def exclude_book(path: str = Form(...)):
    conn = sqlite3.connect(INVENTORY_DB_PATH)
    conn.execute("UPDATE inventory SET excluded = 1 WHERE path = ?", (path,))
    conn.commit()
    conn.close()
    return Response(status_code=200)

@app.get("/books/view")
def view_book(path: str):
    conn = sqlite3.connect(INVENTORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            i.path AS original_path,
            CASE WHEN o.ocr_status LIKE 'ocr_success%' OR i.status = 'native_force_reocred'
                 THEN o.output_path ELSE NULL END AS ocr_output_path,
            CASE WHEN c.status = 'verified_ok'
                 THEN c.output_path ELSE NULL END AS compressed_path
        FROM inventory i
        LEFT JOIN ocr_results o ON i.path = o.path
        LEFT JOIN compress_results c ON c.path = COALESCE(o.output_path, i.path)
        WHERE i.path = ?
    """, (path,))
    row = cursor.fetchone()
    conn.close()

    if row is None:
        return Response(status_code=404, content="Book not found in database")

    candidates = [row["compressed_path"], row["ocr_output_path"], row["original_path"]]
    file_to_serve = next((p for p in candidates if p and os.path.exists(p)), None)

    if file_to_serve is None:
        return Response(status_code=404, content="File not found on disk (is the drive mounted?)")

    return FileResponse(file_to_serve)


@app.get("/recipes/new")
def new_recipe_form(request: Request):
    return templates.TemplateResponse(request=request, name="new_recipe.html", context={})


@app.post("/recipes")
def add_recipe(
    title: str = Form(...),
    source_book: str = Form(""),
    ingredients: str = Form(""),
    instructions: str = Form(""),
):
    create_recipe(title, source_book, ingredients, instructions)
    return RedirectResponse(url="/", status_code=303)


@app.get("/recipes/{recipe_id}")
def recipe_detail(request: Request, recipe_id: int):
    recipe = get_recipe_by_id(recipe_id)
    ingredients = recipe["ingredients"].split("\n")
    instructions = recipe["instructions"].split("\n")
    return templates.TemplateResponse(
        request=request,
        name="recipe_detail.html",
        context={"recipe": recipe, "ingredients": ingredients, "instructions": instructions}
    )


@app.post("/recipes/{recipe_id}/approve")
def approve_recipe(recipe_id: int):
    conn = sqlite3.connect("recipes.db")
    conn.execute("UPDATE recipes SET status = 'approved' WHERE id = ?", (recipe_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/review", status_code=303)


@app.delete("/recipes/{recipe_id}")
def delete_recipe(recipe_id: int):
    conn = sqlite3.connect("recipes.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    conn.commit()
    conn.close()
    return Response(status_code=200)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
