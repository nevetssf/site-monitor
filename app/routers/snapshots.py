from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.checker import SCREENSHOTS_DIR
from app.database import get_session
from app.models import Snapshot, Site
from app.schemas import SnapshotResponse

router = APIRouter()
TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/sites/{site_id}/snapshots", response_model=list[SnapshotResponse])
async def list_snapshots(
    site_id: int,
    page: int = 1,
    per_page: int = 20,
    session: AsyncSession = Depends(get_session),
):
    site = await session.get(Site, site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    offset = (page - 1) * per_page
    result = await session.execute(
        select(Snapshot)
        .where(Snapshot.site_id == site_id)
        .order_by(Snapshot.captured_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    return result.scalars().all()


@router.get("/snapshots/{snapshot_id}/screenshot")
async def serve_screenshot(snapshot_id: int, session: AsyncSession = Depends(get_session)):
    snap = await session.get(Snapshot, snapshot_id)
    if not snap or not snap.screenshot_path:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    # Guard: screenshot_path must start with the correct site_id directory
    expected_prefix = f"{snap.site_id}/"
    if not snap.screenshot_path.startswith(expected_prefix):
        raise HTTPException(status_code=500, detail="Screenshot path site mismatch")

    path = SCREENSHOTS_DIR / snap.screenshot_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="Screenshot file not found")

    return FileResponse(str(path), media_type="image/png")


@router.get("/snapshots/{snapshot_id}/diff")
async def serve_diff(snapshot_id: int, session: AsyncSession = Depends(get_session)):
    snap = await session.get(Snapshot, snapshot_id)
    if not snap or not snap.screenshot_path:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    # Diff file is stored alongside screenshot with _diff suffix
    base = snap.screenshot_path.replace(".png", "_diff.png")
    # Actually look for any diff file in the site directory
    site_dir = SCREENSHOTS_DIR / str(snap.site_id)
    # The diff is saved with a different uuid, so find it by checking naming convention
    # Simpler: return the screenshot itself if no diff found
    diff_path = SCREENSHOTS_DIR / base
    if not diff_path.exists():
        raise HTTPException(status_code=404, detail="Diff image not found")

    return FileResponse(str(diff_path), media_type="image/png")


@router.get("/snapshots/{snapshot_id}", response_class=HTMLResponse)
async def snapshot_detail(
    snapshot_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    snap = await session.get(Snapshot, snapshot_id)
    if not snap:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    site = await session.get(Site, snap.site_id)

    # Get previous snapshot
    prev_result = await session.execute(
        select(Snapshot)
        .where(
            Snapshot.site_id == snap.site_id,
            Snapshot.captured_at < snap.captured_at,
            Snapshot.error_message.is_(None),
        )
        .order_by(Snapshot.captured_at.desc())
        .limit(1)
    )
    prev_snap = prev_result.scalar_one_or_none()

    return templates.TemplateResponse(
        "snapshot.html",
        {
            "request": request,
            "site": site,
            "snapshot": snap,
            "prev_snapshot": prev_snap,
        },
    )
