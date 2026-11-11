import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from db.database import Database, Download


def test_sqlite_user_and_download_statistics(tmp_path):
    async def run():
        db = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite3'}")
        await db.initialize()
        assert await db.upsert_user(111, "tester", "Test") == "active"
        assert await db.upsert_user(111, "tester2", "Test Two") == "active"
        await db.record_download("d-1", 111, "YouTube", "queued", "video")
        await db.record_download("d-1", 111, "YouTube", "succeeded", "video")
        totals = await db.totals()
        assert totals == {"users": 1, "active_users": 1, "downloads": 1, "success": 1, "failed": 0}
        assert await db.set_user_status(111, "blocked") is True
        assert await db.upsert_user(111, "tester2", "Test Two") == "blocked"
        assert await db.set_user_status(999, "blocked") is False
        await db.close()
    asyncio.run(run())


def test_get_user_status(tmp_path):
    async def run():
        db = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite3'}")
        await db.initialize()
        assert await db.get_user_status(42) is None
        await db.upsert_user(42, "u", "U")
        assert await db.get_user_status(42) == "active"
        await db.set_user_status(42, "blocked")
        assert await db.get_user_status(42) == "blocked"
        await db.close()
    asyncio.run(run())


def test_prune_old_records(tmp_path):
    async def run():
        db = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite3'}")
        await db.initialize()
        await db.upsert_user(1, "u", "U")
        await db.record_download("old", 1, "YouTube", "succeeded", "video")
        await db.record_download("new", 1, "Instagram", "queued", "video")
        async with db.sessions() as session:
            row = await session.get(Download, "old")
            row.created_at = datetime.now(timezone.utc) - timedelta(days=40)
            await session.commit()
        deleted = await db.prune_old_records(30)
        assert deleted == 1
        totals = await db.totals()
        assert totals["downloads"] == 1
        with pytest.raises(ValueError):
            await db.prune_old_records(0)
        await db.close()
    asyncio.run(run())


def test_platform_breakdown(tmp_path):
    async def run():
        db = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite3'}")
        await db.initialize()
        await db.upsert_user(1, "u", "U")
        await db.record_download("a", 1, "YouTube", "succeeded", "video")
        await db.record_download("b", 1, "YouTube", "failed", "video")
        await db.record_download("c", 1, "Pinterest", "queued", "video")
        breakdown = await db.platform_breakdown(days=7)
        assert breakdown == {"YouTube": 2, "Pinterest": 1}
        await db.close()
    asyncio.run(run())
