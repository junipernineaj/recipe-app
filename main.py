import os
import sqlite3
from fastapi import FastAPI, Request, Form, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

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
            "SELECT id, title, source_book FROM recipes WHERE title LIKE ?",
            (f"%{search_term}%",)
        )
    else:
        cursor.execute("SELECT id, title, source_book FROM recipes")
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


def create_recipe(title, source_book, ingredients, instructions):
    conn = sqlite3.connect("recipes.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO recipes (title, source_book, ingredients, instructions) VALUES (?, ?, ?, ?)",
        (title, source_book, ingredients, instructions)
    )
    conn.commit()
    conn.close()

def get_books(search_term: str = ""):
    conn = sqlite3.connect(INVENTORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if search_term:
        cursor.execute(
            "SELECT path, filename, status, size_bytes, scanned_at FROM inventory "
            "WHERE status != 'unsupported_extension' AND filename LIKE ? ORDER BY filename",
            (f"%{search_term}%",)
        )
    else:
        cursor.execute(
            "SELECT path, filename, status, size_bytes, scanned_at FROM inventory "
            "WHERE status != 'unsupported_extension' ORDER BY filename"
        )
    books = cursor.fetchall()
    conn.close()
    return books

def get_book_status_counts():
    conn = sqlite3.connect(INVENTORY_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT status, COUNT(*) FROM inventory GROUP BY status ORDER BY COUNT(*) DESC")
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


@app.get("/books")
def books_page(request: Request):
    books = get_books()
    counts = get_book_status_counts()
    return templates.TemplateResponse(
        request=request, name="books.html", context={"books": books, "counts": counts}
    )


@app.get("/books/search")
def books_search(request: Request, q: str = ""):
    books = get_books(q)
    return templates.TemplateResponse(
        request=request, name="books_list.html", context={"books": books}
    )


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
