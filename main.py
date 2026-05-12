from fastapi import FastAPI, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
import os
import uvicorn
from dotenv import load_dotenv

load_dotenv()

from core.database import init_db

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield

app = FastAPI(
    title="Kanban Harness",
    description="AI Team Orchestration Engine — Dashboard API",
    version="0.1.0",
    lifespan=lifespan,
)

from web.api import router as api_router
app.include_router(api_router, prefix="/api")

static_dir = os.path.join(os.path.dirname(__file__), "web", "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
async def index():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"status": "running", "version": "0.1.0"}


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    uvicorn.run("main:app", host=host, port=port, reload=True)
