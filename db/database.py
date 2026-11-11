from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import BigInteger, DateTime, String, ForeignKey, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    status: Mapped[str] = mapped_column(String(16), default="active")


class Download(Base):
    __tablename__ = "downloads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    platform: Mapped[str] = mapped_column(String(24), index=True)
    media_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Database:
    def __init__(self, url: str):
        if url.startswith("sqlite"):
            # Create the directory for relative or absolute SQLite paths.
            db_path = url.split("///", 1)[-1]
            if db_path and db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def initialize(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    async def upsert_user(self, telegram_id: int, username: str | None, first_name: str | None) -> str:
        async with self.sessions() as session:
            user = await session.get(User, telegram_id)
            now = datetime.now(timezone.utc)
            if user is None:
                user = User(telegram_id=telegram_id, username=username, first_name=first_name,
                            registered_at=now, last_activity_at=now, status="active")
                session.add(user)
            else:
                user.username = username
                user.first_name = first_name
                user.last_activity_at = now
            await session.commit()
            return user.status

    async def set_user_status(self, telegram_id: int, status: str) -> bool:
        if status not in {"active", "blocked"}:
            raise ValueError("invalid user status")
        async with self.sessions() as session:
            user = await session.get(User, telegram_id)
            if user is None:
                return False
            user.status = status
            await session.commit()
            return True

    async def get_user_status(self, telegram_id: int) -> str | None:
        """Lightweight status lookup for middleware enforcement."""
        async with self.sessions() as session:
            user = await session.get(User, telegram_id)
            return user.status if user else None

    async def list_users(self, limit: int = 20, offset: int = 0) -> list[User]:
        async with self.sessions() as session:
            result = await session.scalars(select(User).order_by(User.last_activity_at.desc()).limit(limit).offset(offset))
            return list(result)

    async def totals(self) -> dict[str, int]:
        async with self.sessions() as session:
            users = int(await session.scalar(select(func.count()).select_from(User)) or 0)
            active_users = int(await session.scalar(select(func.count()).select_from(User).where(User.status == "active")) or 0)
            downloads = int(await session.scalar(select(func.count()).select_from(Download)) or 0)
            success = int(await session.scalar(select(func.count()).select_from(Download).where(Download.status == "succeeded")) or 0)
            failed = int(await session.scalar(select(func.count()).select_from(Download).where(Download.status == "failed")) or 0)
            return {"users": users, "active_users": active_users, "downloads": downloads, "success": success, "failed": failed}

    async def record_download(self, download_id: str, user_id: int, platform: str, status: str, media_type: str | None = None) -> None:
        if status not in {"queued", "running", "succeeded", "failed", "cancelled"}:
            raise ValueError("invalid download status")
        async with self.sessions() as session:
            row = await session.get(Download, download_id)
            now = datetime.now(timezone.utc)
            if row is None:
                row = Download(id=download_id, user_id=user_id, platform=platform, status=status, media_type=media_type,
                               created_at=now, completed_at=now if status in {"succeeded", "failed", "cancelled"} else None)
                session.add(row)
            else:
                row.status = status
                row.completed_at = now if status in {"succeeded", "failed", "cancelled"} else None
            await session.commit()

    async def platform_breakdown(self, days: int = 7) -> dict[str, int]:
        """Download counts per platform within the last ``days`` days."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        async with self.sessions() as session:
            result = await session.execute(
                select(Download.platform, func.count())
                .where(Download.created_at >= cutoff)
                .group_by(Download.platform)
            )
            return {platform: int(count) for platform, count in result.all()}

    async def prune_old_records(self, days: int) -> int:
        """Delete download records older than the retention window. Returns deleted count."""
        if days < 1:
            raise ValueError("retention must be at least one day")
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        async with self.sessions() as session:
            result = await session.execute(delete(Download).where(Download.created_at < cutoff))
            await session.commit()
            return int(result.rowcount or 0)
