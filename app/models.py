from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    check_interval: Mapped[int] = mapped_column(Integer, default=300)
    jitter_pct: Mapped[int] = mapped_column(Integer, default=10)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    ntfy_topic: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(20), default="pending")
    unavailable_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    unavailable_notified: Mapped[bool] = mapped_column(Boolean, default=False)

    snapshots: Mapped[list["Snapshot"]] = relationship(
        "Snapshot", back_populates="site", cascade="all, delete-orphan"
    )


class Snapshot(Base):
    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(Integer, ForeignKey("sites.id", ondelete="CASCADE"))
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    screenshot_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    html_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    html_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    changed: Mapped[bool] = mapped_column(Boolean, default=False)
    diff_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    site: Mapped["Site"] = relationship("Site", back_populates="snapshots")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
