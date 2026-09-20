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

def get_recipe_by_id(recipe_id: int):
    conn = sqlite3.connect("recipes.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,))
    recipe = cursor.fetchone()
    conn.close()
    return recipe

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
