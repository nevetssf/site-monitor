from datetime import timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.checker import check_site, SCREENSHOTS_DIR
from app.database import get_session
from app.models import Site, Snapshot
from app.schemas import SiteCreate, SiteResponse, SiteUpdate
from app.scheduler import add_or_replace_job, remove_job, get_next_run_time

router = APIRouter()
TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _latest_screenshot(site: Site) -> str | None:
    if not site.snapshots:
        return None
    valid = [s for s in site.snapshots if s.screenshot_path and not s.error_message]
    if not valid:
        return None
    latest = max(valid, key=lambda s: s.captured_at)
    return f"/snapshots/{latest.id}/screenshot"


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Site).order_by(Site.created_at.desc())
    )
    sites = result.scalars().all()

    # Load latest snapshot per site; build JSON-serializable list for Alpine
    site_data = []
    for site in sites:
        snap_result = await session.execute(
            select(Snapshot)
            .where(Snapshot.site_id == site.id, Snapshot.screenshot_path.isnot(None))
            .order_by(Snapshot.captured_at.desc())
            .limit(1)
        )
        latest_snap = snap_result.scalar_one_or_none()
        screenshot_url = f"/snapshots/{latest_snap.id}/screenshot" if latest_snap else None
        site_data.append({
            "site": {
                "id": site.id,
                "name": site.name,
                "url": site.url,
                "check_interval": site.check_interval,
                "is_active": site.is_active,
                "ntfy_topic": site.ntfy_topic,
                "last_checked_at": site.last_checked_at.isoformat() if site.last_checked_at else None,
                "last_changed_at": site.last_changed_at.isoformat() if site.last_changed_at else None,
                "last_status": site.last_status,
            },
            "screenshot_url": screenshot_url,
        })

    return templates.TemplateResponse(
        "index.html", {"request": request, "site_data": site_data}
    )


@router.get("/sites", response_model=list[SiteResponse])
async def list_sites(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Site).order_by(Site.created_at.desc()))
    sites = result.scalars().all()

    responses = []
    for site in sites:
        snap_result = await session.execute(
            select(Snapshot)
            .where(Snapshot.site_id == site.id, Snapshot.screenshot_path.isnot(None))
            .order_by(Snapshot.captured_at.desc())
            .limit(1)
        )
        latest_snap = snap_result.scalar_one_or_none()
        r = SiteResponse.model_validate(site)
        r.latest_screenshot = f"/snapshots/{latest_snap.id}/screenshot" if latest_snap else None
        responses.append(r)

    return responses


@router.post("/sites", response_model=SiteResponse, status_code=201)
async def create_site(
    body: SiteCreate,
    session: AsyncSession = Depends(get_session),
):
    existing = await session.execute(select(Site).where(Site.url == body.url))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="URL already monitored")

    site = Site(**body.model_dump())
    session.add(site)
    await session.commit()
    await session.refresh(site)

    if site.is_active:
        add_or_replace_job(site.id, site.check_interval, site.jitter_pct)

    return site


@router.get("/sites/{site_id}", response_class=HTMLResponse)
async def site_detail(
    site_id: int, request: Request, session: AsyncSession = Depends(get_session)
):
    site = await session.get(Site, site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    snap_result = await session.execute(
        select(Snapshot)
        .where(Snapshot.site_id == site_id)
        .order_by(Snapshot.captured_at.desc())
        .limit(500)
    )
    snapshots = snap_result.scalars().all()

    latest_screenshot = None
    for snap in snapshots:
        if snap.screenshot_path and not snap.error_message:
            latest_screenshot = f"/snapshots/{snap.id}/screenshot"
            break

    next_run = get_next_run_time(site_id)
    next_run_utc = next_run.astimezone(timezone.utc) if next_run else None

    return templates.TemplateResponse(
        "site_detail.html",
        {
            "request": request,
            "site": site,
            "snapshots": snapshots,
            "latest_screenshot": latest_screenshot,
            "next_run_time": next_run_utc.strftime("%Y-%m-%dT%H:%M:%S") if next_run_utc else None,
        },
    )


@router.put("/sites/{site_id}", response_model=SiteResponse)
async def update_site(
    site_id: int,
    body: SiteUpdate,
    session: AsyncSession = Depends(get_session),
):
    site = await session.get(Site, site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(site, field, value)

    await session.commit()
    await session.refresh(site)

    if site.is_active:
        add_or_replace_job(site.id, site.check_interval, site.jitter_pct)
    else:
        remove_job(site.id)

    return site


@router.delete("/sites/{site_id}", status_code=204)
async def delete_site(site_id: int, session: AsyncSession = Depends(get_session)):
    site = await session.get(Site, site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    remove_job(site_id)

    # Delete screenshot files
    site_dir = SCREENSHOTS_DIR / str(site_id)
    if site_dir.exists():
        import shutil
        shutil.rmtree(site_dir, ignore_errors=True)

    await session.delete(site)
    await session.commit()


@router.post("/sites/{site_id}/toggle")
async def toggle_site(site_id: int, session: AsyncSession = Depends(get_session)):
    site = await session.get(Site, site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    site.is_active = not site.is_active
    await session.commit()
    await session.refresh(site)

    if site.is_active:
        add_or_replace_job(site.id, site.check_interval, site.jitter_pct)
    else:
        remove_job(site.id)

    return {"id": site.id, "is_active": site.is_active}


@router.post("/sites/{site_id}/check-now", status_code=202)
async def check_now(
    site_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    site = await session.get(Site, site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    background_tasks.add_task(check_site, site_id)
    return {"status": "queued", "site_id": site_id}
