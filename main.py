import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import traceback
from fastapi import FastAPI
from database import engine
from models import Base
from routes import login

app = FastAPI()

Base.metadata.create_all(bind=engine)

app.include_router(login.router)

@app.get("/")
def home():
    return {"status": "Guardian Shield API rodando"}

from routes import admin
app.include_router(admin.router)