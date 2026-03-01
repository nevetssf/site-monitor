from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session, DEFAULT_SETTINGS
from app.models import Setting
from app.schemas import SettingsUpdate

router = APIRouter()
TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

SETTING_KEYS = list(DEFAULT_SETTINGS.keys())


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session: AsyncSession = Depends(get_session)):
    current = {}
    for key in SETTING_KEYS:
        row = await session.get(Setting, key)
        current[key] = row.value if row else DEFAULT_SETTINGS.get(key, "")

    return templates.TemplateResponse(
        "settings.html", {"request": request, "settings": current}
    )


@router.get("/api/settings")
async def get_settings(session: AsyncSession = Depends(get_session)):
    result = {}
    for key in SETTING_KEYS:
        row = await session.get(Setting, key)
        result[key] = row.value if row else DEFAULT_SETTINGS.get(key, "")
    return result


@router.put("/api/settings")
async def update_settings(
    body: SettingsUpdate, session: AsyncSession = Depends(get_session)
):
    updates = body.model_dump(exclude_none=True)
    for key, value in updates.items():
        if key not in SETTING_KEYS:
            continue
        row = await session.get(Setting, key)
        if row:
            row.value = value
        else:
            session.add(Setting(key=key, value=value))

    await session.commit()
    return {"status": "updated", "keys": list(updates.keys())}


class TestNtfyBody(BaseModel):
    topic_override: str | None = None


@router.post("/api/settings/test-ntfy")
async def test_ntfy(body: TestNtfyBody = TestNtfyBody(), session: AsyncSession = Depends(get_session)):
    server_row = await session.get(Setting, "ntfy_server")
    topic_row = await session.get(Setting, "ntfy_topic")

    server = server_row.value if server_row else DEFAULT_SETTINGS["ntfy_server"]
    topic = body.topic_override or (topic_row.value if topic_row else DEFAULT_SETTINGS["ntfy_topic"])

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{server.rstrip('/')}/{topic}",
                content="Site Monitor test notification",
                headers={
                    "Title": "Site Monitor: Test",
                    "Tags": "white_check_mark",
                    "Priority": "default",
                },
                timeout=10,
            )
        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"ntfy returned {resp.status_code}")
        return {"status": "sent", "server": server, "topic": topic}
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
