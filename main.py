import sqlite3
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates

app = FastAPI()
templates = Jinja2Templates(directory="templates")

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
