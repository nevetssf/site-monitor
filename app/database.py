from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.models import Base, Setting

DATA_DIR = Path("/app/data")
DB_PATH = DATA_DIR / "monitor.db"

# Use local data dir when not in Docker
if not DATA_DIR.exists():
    DATA_DIR = Path(__file__).parent.parent / "data"
    DB_PATH = DATA_DIR / "monitor.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "screenshots").mkdir(exist_ok=True)

engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}", echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

DEFAULT_SETTINGS = {
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "site-monitor",
    "screenshot_retention_days": "7",
    "request_timeout": "30",
    "pixel_diff_threshold": "1.0",
}


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Migrate: add jitter_pct column if it doesn't exist (existing DBs)
        try:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "ALTER TABLE sites ADD COLUMN jitter_pct INTEGER DEFAULT 10"
                )
            )
        except Exception:
            pass  # Column already exists

    async with AsyncSessionLocal() as session:
        for key, value in DEFAULT_SETTINGS.items():
            existing = await session.get(Setting, key)
            if existing is None:
                session.add(Setting(key=key, value=value))
        await session.commit()


async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def get_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.get(Setting, key)
    return row.value if row else DEFAULT_SETTINGS.get(key)
