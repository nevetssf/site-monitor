import hashlib
import io
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from PIL import Image, ImageChops
from playwright.async_api import async_playwright
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal, DATA_DIR, get_setting
from app.models import Site, Snapshot

logger = logging.getLogger(__name__)

SCREENSHOTS_DIR = DATA_DIR / "screenshots"


async def _capture_page(url: str, timeout_s: int) -> tuple[bytes, str]:
    """Returns (png_bytes, html_content)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        try:
            page = await browser.new_page()
            # Use domcontentloaded — fires once HTML is parsed, doesn't wait
            # for images/fonts/subframes. Much more reliable than load/networkidle
            # on JS-heavy or slow sites.
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            # Give JS a few seconds to render dynamic content
            await page.wait_for_timeout(3000)
            png_bytes = await page.screenshot(full_page=True)
            html = await page.content()
            return png_bytes, html
        finally:
            await browser.close()


def _pixel_diff(img_bytes_a: bytes, img_bytes_b: bytes) -> float:
    """Returns percentage of pixels that differ."""
    img_a = Image.open(io.BytesIO(img_bytes_a)).convert("RGB")
    img_b = Image.open(io.BytesIO(img_bytes_b)).convert("RGB")

    # Resize to same dimensions if needed
    if img_a.size != img_b.size:
        img_b = img_b.resize(img_a.size, Image.LANCZOS)

    diff = ImageChops.difference(img_a, img_b)
    total_pixels = img_a.width * img_a.height
    diff_pixels = sum(1 for px in diff.getdata() if any(c > 10 for c in px))
    return (diff_pixels / total_pixels) * 100


def _make_diff_image(img_bytes_a: bytes, img_bytes_b: bytes) -> bytes:
    """Creates a highlighted diff PNG."""
    img_a = Image.open(io.BytesIO(img_bytes_a)).convert("RGB")
    img_b = Image.open(io.BytesIO(img_bytes_b)).convert("RGB")

    if img_a.size != img_b.size:
        img_b = img_b.resize(img_a.size, Image.LANCZOS)

    diff = ImageChops.difference(img_a, img_b)
    # Enhance diff visibility
    enhanced = diff.point(lambda x: min(255, x * 10))

    out = io.BytesIO()
    enhanced.save(out, format="PNG")
    return out.getvalue()


async def _send_ntfy_alert(
    ntfy_server: str,
    topic: str,
    site_name: str,
    url: str,
    diff_score: float | None,
) -> None:
    summary = f"{diff_score:.1f}% pixels changed" if diff_score is not None else "HTML content changed"
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{ntfy_server.rstrip('/')}/{topic}",
                content=f"{url}\n{summary}",
                headers={
                    "Title": f"Site Changed: {site_name}",
                    "Click": url,
                    "Tags": "eyes",
                    "Priority": "default",
                },
                timeout=10,
            )
    except Exception as exc:
        logger.warning("ntfy alert failed: %s", exc)


async def check_site(site_id: int) -> None:
    async with AsyncSessionLocal() as session:
        site = await session.get(Site, site_id)
        if site is None or not site.is_active:
            return

        timeout_s = int(await get_setting(session, "request_timeout") or 30)
        threshold = float(await get_setting(session, "pixel_diff_threshold") or 1.0)
        ntfy_server = await get_setting(session, "ntfy_server") or "https://ntfy.sh"
        global_topic = await get_setting(session, "ntfy_topic") or "site-monitor"
        effective_topic = site.ntfy_topic or global_topic

        # Load previous snapshot (most recent without error)
        prev_result = await session.execute(
            select(Snapshot)
            .where(Snapshot.site_id == site_id, Snapshot.error_message.is_(None))
            .order_by(Snapshot.captured_at.desc())
            .limit(1)
        )
        prev_snapshot = prev_result.scalar_one_or_none()


        error_message = None
        png_bytes = None
        html_hash = ""
        html_size = None
        changed = False
        diff_score = None
        screenshot_path = None

        try:
            png_bytes, html = await _capture_page(site.url, timeout_s)
            html_hash = hashlib.sha256(html.encode()).hexdigest()
            html_size = len(html)

            if prev_snapshot is None:
                # Baseline — first capture
                changed = False
                diff_score = None
            elif prev_snapshot.html_hash == html_hash:
                changed = False
                diff_score = 0.0
            else:
                # HTML changed — do pixel diff against previous screenshot
                if prev_snapshot and prev_snapshot.screenshot_path:
                    prev_path = SCREENSHOTS_DIR / prev_snapshot.screenshot_path
                    if prev_path.exists():
                        prev_bytes = prev_path.read_bytes()
                        diff_score = _pixel_diff(prev_bytes, png_bytes)
                        changed = diff_score >= threshold
                    else:
                        changed = True
                else:
                    changed = True

            # Save screenshot for every capture
            site_dir = SCREENSHOTS_DIR / str(site_id)
            site_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{uuid.uuid4()}.png"
            (site_dir / filename).write_bytes(png_bytes)
            screenshot_path = f"{site_id}/{filename}"

            # Save diff image if changed
            if changed and prev_snapshot and prev_snapshot.screenshot_path:
                prev_path = SCREENSHOTS_DIR / prev_snapshot.screenshot_path
                if prev_path.exists():
                    diff_bytes = _make_diff_image(prev_path.read_bytes(), png_bytes)
                    diff_filename = f"{uuid.uuid4()}_diff.png"
                    (site_dir / diff_filename).write_bytes(diff_bytes)

            if changed:
                await _send_ntfy_alert(ntfy_server, effective_topic, site.name, site.url, diff_score)

        except Exception as exc:
            logger.error("Error checking site %s: %s", site.url, exc)
            error_message = str(exc)

        # Write snapshot
        now = datetime.utcnow()
        snapshot = Snapshot(
            site_id=site_id,
            captured_at=now,
            screenshot_path=screenshot_path,
            html_hash=html_hash or "error",
            html_size=html_size,
            changed=changed,
            diff_score=diff_score,
            error_message=error_message,
        )
        session.add(snapshot)

        # Update site
        site.last_checked_at = now
        if error_message:
            site.last_status = "error"
        elif changed:
            site.last_status = "changed"
            site.last_changed_at = now
        else:
            site.last_status = "unchanged"

        await session.commit()
        await _cleanup_old_snapshots(session, site_id)


async def _cleanup_old_snapshots(session: AsyncSession, site_id: int) -> None:
    retention_days = int(await get_setting(session, "screenshot_retention_days") or 30)
    cutoff = datetime.utcnow() - timedelta(days=retention_days)

    old = await session.execute(
        select(Snapshot)
        .where(Snapshot.site_id == site_id, Snapshot.captured_at < cutoff)
    )
    for snap in old.scalars():
        if snap.screenshot_path:
            path = SCREENSHOTS_DIR / snap.screenshot_path
            if path.exists():
                path.unlink(missing_ok=True)
        await session.delete(snap)

    await session.commit()
