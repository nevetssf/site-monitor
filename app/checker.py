import hashlib
import io
import logging
import re
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

_ERROR_PAGE_PATTERNS = re.compile(
    r"chrome-error://|"
    r"neterror|"
    r"ERR_NAME_NOT_RESOLVED|"
    r"ERR_CONNECTION_REFUSED|"
    r"ERR_CONNECTION_TIMED_OUT|"
    r"ERR_CONNECTION_RESET|"
    r"ERR_INTERNET_DISCONNECTED|"
    r"ERR_SSL_PROTOCOL_ERROR|"
    r"ERR_CERT_|"
    r"This site can[\u2019']t be reached|"
    r"There was a problem loading this website",
    re.IGNORECASE,
)


async def _capture_page(url: str, timeout_s: int) -> tuple[bytes, str, int | None]:
    """Returns (png_bytes, html_content, http_status_code)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        try:
            page = await browser.new_page()
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            await page.wait_for_timeout(3000)
            png_bytes = await page.screenshot(full_page=True)
            html = await page.content()
            status_code = response.status if response else None
            return png_bytes, html, status_code
        finally:
            await browser.close()


def _is_error_page(html: str, status_code: int | None) -> bool:
    """Detect browser error pages and failed navigations."""
    if status_code is None or status_code >= 400:
        if _ERROR_PAGE_PATTERNS.search(html):
            return True
    return False


def _pixel_diff(img_bytes_a: bytes, img_bytes_b: bytes) -> float:
    """Returns percentage of pixels that differ."""
    img_a = Image.open(io.BytesIO(img_bytes_a)).convert("RGB")
    img_b = Image.open(io.BytesIO(img_bytes_b)).convert("RGB")

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


async def _send_ntfy_down_alert(
    ntfy_server: str, topic: str, site_name: str, url: str, hours_down: float
) -> None:
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{ntfy_server.rstrip('/')}/{topic}",
                content=f"{url}\nDown for {hours_down:.0f}+ hours",
                headers={
                    "Title": f"Site Down: {site_name}",
                    "Click": url,
                    "Tags": "warning",
                    "Priority": "high",
                },
                timeout=10,
            )
    except Exception as exc:
        logger.warning("ntfy down alert failed: %s", exc)


async def _send_ntfy_recovery_alert(
    ntfy_server: str, topic: str, site_name: str, url: str
) -> None:
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{ntfy_server.rstrip('/')}/{topic}",
                content=f"{url}\nSite is back online",
                headers={
                    "Title": f"Site Recovered: {site_name}",
                    "Click": url,
                    "Tags": "white_check_mark",
                    "Priority": "default",
                },
                timeout=10,
            )
    except Exception as exc:
        logger.warning("ntfy recovery alert failed: %s", exc)


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
        unavailable_notify_hours = float(
            await get_setting(session, "unavailable_notify_hours") or 24
        )

        error_message = None
        png_bytes = None
        html_hash = ""
        html_size = None
        changed = False
        diff_score = None
        screenshot_path = None
        now = datetime.utcnow()

        try:
            png_bytes, html, status_code = await _capture_page(site.url, timeout_s)
            html_hash = hashlib.sha256(html.encode()).hexdigest()
            html_size = len(html)

            # Save screenshot
            site_dir = SCREENSHOTS_DIR / str(site_id)
            site_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{uuid.uuid4()}.png"
            (site_dir / filename).write_bytes(png_bytes)
            screenshot_path = f"{site_id}/{filename}"

            if _is_error_page(html, status_code):
                # --- Site is unavailable ---
                error_message = "Website unavailable"
                was_unavailable = site.last_status == "unavailable"

                if not was_unavailable:
                    site.unavailable_since = now
                    site.unavailable_notified = False

                site.last_status = "unavailable"
                site.last_checked_at = now

                # Check if we should send a down notification
                if (
                    site.unavailable_since
                    and not site.unavailable_notified
                    and (now - site.unavailable_since) >= timedelta(hours=unavailable_notify_hours)
                ):
                    await _send_ntfy_down_alert(
                        ntfy_server, effective_topic, site.name, site.url,
                        (now - site.unavailable_since).total_seconds() / 3600,
                    )
                    site.unavailable_notified = True

                snapshot = Snapshot(
                    site_id=site_id,
                    captured_at=now,
                    screenshot_path=screenshot_path,
                    html_hash=html_hash,
                    html_size=html_size,
                    changed=False,
                    diff_score=None,
                    error_message=error_message,
                )
                session.add(snapshot)
                await session.commit()
                await _cleanup_old_snapshots(session, site_id)
                return

            # --- Site is up ---
            # If recovering from unavailable, send recovery alert
            if site.last_status == "unavailable":
                await _send_ntfy_recovery_alert(
                    ntfy_server, effective_topic, site.name, site.url
                )
                site.unavailable_since = None
                site.unavailable_notified = False

            # Load previous snapshot (most recent without error)
            prev_result = await session.execute(
                select(Snapshot)
                .where(Snapshot.site_id == site_id, Snapshot.error_message.is_(None))
                .order_by(Snapshot.captured_at.desc())
                .limit(1)
            )
            prev_snapshot = prev_result.scalar_one_or_none()

            if prev_snapshot is None:
                changed = False
                diff_score = None
            elif prev_snapshot.html_hash == html_hash:
                changed = False
                diff_score = 0.0
            else:
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
