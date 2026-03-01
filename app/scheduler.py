import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.checker import check_site

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


def _job_id(site_id: int) -> str:
    return f"site_{site_id}"


def add_or_replace_job(site_id: int, interval_seconds: int) -> None:
    job_id = _job_id(site_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        check_site,
        trigger=IntervalTrigger(seconds=interval_seconds),
        id=job_id,
        args=[site_id],
        replace_existing=True,
        misfire_grace_time=60,
    )
    logger.info("Scheduled job %s every %ds", job_id, interval_seconds)


def remove_job(site_id: int) -> None:
    job_id = _job_id(site_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logger.info("Removed job %s", job_id)
