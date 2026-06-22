"""FastAPI application entrypoint.

Run with:  uvicorn app.main:app --host 0.0.0.0 --port 8000
(from the ncaa-search/ directory)
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import router as api_router
from app.db import init_db

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_ROOT / "web"

app = FastAPI(title="NCAA Player Ranking Search", version="1.0")
app.include_router(api_router)


@app.on_event("startup")
def _startup():
    init_db()


@app.get("/")
def index():
    return FileResponse(str(WEB_DIR / "index.html"))


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# Serve static assets (app.js, etc.). Mounted last so /api and / take priority.
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
