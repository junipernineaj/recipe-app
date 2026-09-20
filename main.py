import sqlite3
from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse
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

def create_recipe(title, source_book, ingredients, instructions):
    conn = sqlite3.connect("recipes.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO recipes (title, source_book, ingredients, instructions) VALUES (?, ?, ?, ?)",
        (title, source_book, ingredients, instructions)
    )
    conn.commit()
    conn.close()

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
