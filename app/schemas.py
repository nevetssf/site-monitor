from datetime import datetime
from pydantic import BaseModel, HttpUrl


class SiteCreate(BaseModel):
    name: str
    url: str
    check_interval: int = 300
    is_active: bool = True
    ntfy_topic: str | None = None


class SiteUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    check_interval: int | None = None
    is_active: bool | None = None
    ntfy_topic: str | None = None


class SiteResponse(BaseModel):
    id: int
    name: str
    url: str
    check_interval: int
    is_active: bool
    ntfy_topic: str | None
    created_at: datetime
    last_checked_at: datetime | None
    last_changed_at: datetime | None
    last_status: str
    latest_screenshot: str | None = None

    model_config = {"from_attributes": True}


class SnapshotResponse(BaseModel):
    id: int
    site_id: int
    captured_at: datetime
    screenshot_path: str | None
    html_hash: str
    html_size: int | None
    changed: bool
    diff_score: float | None
    error_message: str | None

    model_config = {"from_attributes": True}


class SettingsUpdate(BaseModel):
    ntfy_server: str | None = None
    ntfy_topic: str | None = None
    screenshot_retention_days: str | None = None
    request_timeout: str | None = None
    pixel_diff_threshold: str | None = None
