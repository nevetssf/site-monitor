import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.checker import check_site

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


def _job_id(site_id: int) -> str:
    return f"site_{site_id}"


def add_or_replace_job(site_id: int, interval_seconds: int, jitter_pct: int = 0) -> None:
    job_id = _job_id(site_id)
    jitter_seconds = int(interval_seconds * jitter_pct / 100)
    scheduler.add_job(
        check_site,
        trigger=IntervalTrigger(seconds=interval_seconds, jitter=jitter_seconds),
        id=job_id,
        args=[site_id],
        replace_existing=True,
        misfire_grace_time=60,
    )
    logger.info("Scheduled job %s every %ds jitter±%ds", job_id, interval_seconds, jitter_seconds)


def remove_job(site_id: int) -> None:
    job_id = _job_id(site_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logger.info("Removed job %s", job_id)


def get_next_run_time(site_id: int) -> datetime | None:
    job = scheduler.get_job(_job_id(site_id))
    if job and job.next_run_time:
        return job.next_run_time
    return None
