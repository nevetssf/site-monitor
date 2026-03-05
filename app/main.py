from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
from sqlalchemy import select

from app.database import init_db, AsyncSessionLocal
from app.models import Site
from app.scheduler import scheduler, add_or_replace_job
from app.routers import sites, snapshots, settings as settings_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _filesizeformat(value: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


templates.env.filters["filesizeformat"] = _filesizeformat


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    # Load active sites and schedule jobs
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Site).where(Site.is_active == True))
        active_sites = result.scalars().all()
        for site in active_sites:
            add_or_replace_job(site.id, site.check_interval, site.jitter_pct)

    scheduler.start()
    logger.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))

    yield

    scheduler.shutdown(wait=False)
    logger.info("Scheduler shut down")


app = FastAPI(title="Site Monitor", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(sites.router)
app.include_router(snapshots.router)
app.include_router(settings_router.router)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
