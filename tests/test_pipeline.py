"""End-to-end pipeline tests with lightweight Telegram fakes.

Covers: success flow with DB lifecycle recording, duplicate rejection,
per-user busy rejection (queued jobs count too), cancel-button ownership,
HTML-escaped titles/captions, and the image-set (carousel) pipeline.
"""
import asyncio
from pathlib import Path

import pytest

from bot.handlers.common import start_download, start_image_download, cancel_download
from db.database import Database
from services.youtube import DownloadedMedia, FormatChoice
from workers.queue_manager import DownloadQueue, JobState


class FakeUser:
    def __init__(self, uid: int):
        self.id = uid


class FakeMessage:
    def __init__(self, user_id: int = 1):
        self.from_user = FakeUser(user_id)
        self.edits: list[tuple[str, dict]] = []
        self.sent: list[tuple[str, dict]] = []
        self.children: list["FakeMessage"] = []
        self.deleted = False
        self.video_sent: list[dict] = []
        self.audio_sent: list[dict] = []
        self.photos_sent: list[dict] = []
        self.albums_sent: list[list] = []

    async def answer(self, text=None, **kwargs):
        child = FakeMessage(self.from_user.id)
        child.sent.append((text, kwargs))
        self.children.append(child)
        return child

    async def answer_video(self, *args, **kwargs):
        self.video_sent.append({"args": args, "kwargs": kwargs})

    async def answer_audio(self, *args, **kwargs):
        self.audio_sent.append({"args": args, "kwargs": kwargs})

    async def answer_photo(self, *args, **kwargs):
        self.photos_sent.append({"args": args, "kwargs": kwargs})

    async def answer_media_group(self, media=None, **kwargs):
        self.albums_sent.append(list(media or []))

    async def edit_text(self, text=None, **kwargs):
        self.edits.append((text, kwargs))

    async def delete(self):
        self.deleted = True

    def last_markup(self):
        for _, kwargs in reversed(self.edits):
            if "reply_markup" in kwargs and kwargs["reply_markup"] is not None:
                return kwargs["reply_markup"]
        return None


class FakeQuery:
    def __init__(self, user_id: int, message: FakeMessage, data: str = ""):
        self.from_user = FakeUser(user_id)
        self.message = message
        self.data = data
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text or "", show_alert))


CHOICE = FormatChoice("720", "ویدیو تا 720p", "best[height<=720]", 720)


def fake_downloader(url, choice, directory, progress, max_bytes):
    async def run():
        if progress:
            progress({"status": "downloading", "percent": 50.0, "speed": 2048.0, "eta": 2, "downloaded_bytes": 50, "total_bytes_estimate": 100})
        out = Path(directory) / "out.mp4"
        out.write_bytes(b"x" * 128)
        return DownloadedMedia(out, "test media", 128, choice)
    return run()


async def _wait_done(queue: DownloadQueue):
    await queue._queue.join()
    await asyncio.sleep(0)  # let on_finish side effects settle


def test_success_flow_records_full_lifecycle(tmp_path):
    async def run():
        db = Database(f"sqlite+aiosqlite:///{tmp_path / 't.sqlite3'}")
        await db.initialize()
        await db.upsert_user(10, "u", "U")
        queue = DownloadQueue(max_waiting=2)
        await queue.start()
        message = FakeMessage(10)
        query = FakeQuery(10, message)

        await start_download(query, url="https://www.youtube.com/watch?v=ok", title="t",
                             choice=CHOICE, platform="YouTube", downloader=fake_downloader,
                             queue=queue, db=db, max_bytes=1024 * 1024)
        await _wait_done(queue)
        await queue.close()

        statuses = {}
        async with db.sessions() as session:
            from db.database import Download
            from sqlalchemy import select
            for row in (await session.scalars(select(Download))).all():
                statuses[row.id] = row.status
        assert list(statuses.values()) == ["succeeded"]
        # Status message got queue confirmation with a cancel keyboard.
        status = message.children[-1]
        text, kwargs = status.edits[0]
        assert "درخواست ثبت شد" in text
        assert kwargs["reply_markup"] is not None
        await db.close()
    asyncio.run(run())


def test_video_caption_is_html_escaped(tmp_path):
    """Titles with &, <, > must not reach Telegram as raw HTML."""
    async def run():
        db = Database(f"sqlite+aiosqlite:///{tmp_path / 't.sqlite3'}")
        await db.initialize()
        queue = DownloadQueue(max_waiting=2)
        await queue.start()
        message = FakeMessage(3)
        query = FakeQuery(3, message)
        tricky = 'AC/DC & Guns <Live> "Rock"'

        await start_download(query, url="https://www.youtube.com/watch?v=esc", title=tricky,
                             choice=CHOICE, platform="YouTube", downloader=fake_downloader,
                             queue=queue, db=db, max_bytes=1024 * 1024)
        await _wait_done(queue)
        await queue.close()

        assert message.video_sent, "video must be delivered"
        caption = message.video_sent[0]["kwargs"]["caption"]
        assert "&amp;" in caption and "&lt;" in caption and "&quot;" in caption
        assert "& " not in caption.replace("&amp;", "").replace("&lt;", "").replace("&quot;", "")
        await db.close()
    asyncio.run(run())


def test_duplicate_url_rejected(tmp_path):
    async def run():
        db = Database(f"sqlite+aiosqlite:///{tmp_path / 't.sqlite3'}")
        await db.initialize()
        # No worker: the first job stays QUEUED, which must be enough for the
        # duplicate fingerprint check to reject the second submission.
        queue = DownloadQueue(max_waiting=2)

        q1 = FakeQuery(1, FakeMessage(1))
        await start_download(q1, url="https://www.youtube.com/watch?v=dup", title="t",
                             choice=CHOICE, platform="YouTube", downloader=fake_downloader,
                             queue=queue, db=db, max_bytes=1024 * 1024)
        q2 = FakeQuery(2, FakeMessage(2))
        await start_download(q2, url="https://www.youtube.com/watch?v=dup", title="t",
                             choice=CHOICE, platform="YouTube", downloader=fake_downloader,
                             queue=queue, db=db, max_bytes=1024 * 1024)
        text, _ = q2.message.children[-1].edits[-1]
        assert "در صف دانلود" in text
        await queue.close()
        await db.close()
    asyncio.run(run())


def test_busy_user_rejected(tmp_path):
    async def run():
        db = Database(f"sqlite+aiosqlite:///{tmp_path / 't.sqlite3'}")
        await db.initialize()
        queue = DownloadQueue(max_waiting=2)
        # No worker: the user's first job stays QUEUED and must already make
        # the next request from the same user "busy" (queued jobs count too).
        first = FakeQuery(5, FakeMessage(5))
        await start_download(first, url="https://www.youtube.com/watch?v=busy1", title="t",
                             choice=CHOICE, platform="YouTube", downloader=fake_downloader,
                             queue=queue, db=db, max_bytes=1024 * 1024)
        query = FakeQuery(5, FakeMessage(5))
        await start_download(query, url="https://www.youtube.com/watch?v=busy2", title="t",
                             choice=CHOICE, platform="YouTube", downloader=fake_downloader,
                             queue=queue, db=db, max_bytes=1024 * 1024)
        text, _ = query.message.children[-1].edits[-1]
        assert "یک دانلود فعال دارید" in text
        await queue.close()
        await db.close()
    asyncio.run(run())


def test_cancel_button_ownership(tmp_path):
    async def run():
        db = Database(f"sqlite+aiosqlite:///{tmp_path / 't.sqlite3'}")
        await db.initialize()
        queue = DownloadQueue(max_waiting=2)  # no worker: job remains queued
        owner = FakeMessage(7)
        await start_download(FakeQuery(7, owner), url="https://www.youtube.com/watch?v=c", title="t",
                             choice=CHOICE, platform="YouTube", downloader=fake_downloader,
                             queue=queue, db=db, max_bytes=1024 * 1024)
        markup = owner.children[-1].last_markup()
        job_id = markup.inline_keyboard[0][0].callback_data.split(":")[2]

        # A different user cannot cancel it.
        stranger_query = FakeQuery(99, FakeMessage(99), data=f"dl:cancel:{job_id}")
        await cancel_download(stranger_query, queue)
        assert stranger_query.answers[-1][0] == "این درخواست متعلق به کاربر دیگری است."

        # The owner cancels; the queued job is marked cancelled.
        owner_query = FakeQuery(7, FakeMessage(7), data=f"dl:cancel:{job_id}")
        await cancel_download(owner_query, queue)
        assert owner_query.answers[-1][0] == "درخواست لغو ثبت شد."
        assert queue.get(job_id).state == JobState.CANCELLED
        await queue.close()
        await db.close()
    asyncio.run(run())


class FakeImageSet:
    def __init__(self, directory: str, count: int, title: str = "کپشن تست & <خطرناک>"):
        self.paths = []
        for i in range(count):
            p = Path(directory) / f"img_{i:02d}.jpg"
            p.write_bytes(b"jpeg" * 10)
            self.paths.append(p)
        self.title = title
        self.total_size = count * 40


def image_fetcher(directory_holder):
    async def run(url, directory, max_total_bytes):
        directory_holder.append(directory)
        return FakeImageSet(directory, count=3)
    return run


def test_image_set_flow_sends_album_and_records_success(tmp_path):
    async def run():
        db = Database(f"sqlite+aiosqlite:///{tmp_path / 't.sqlite3'}")
        await db.initialize()
        queue = DownloadQueue(max_waiting=2)
        await queue.start()
        message = FakeMessage(11)
        query = FakeQuery(11, message)
        holder: list[str] = []

        await start_image_download(query, url="https://www.instagram.com/p/ABC123xyz/",
                                   title="carousel", platform="Instagram",
                                   fetcher=image_fetcher(holder), queue=queue, db=db,
                                   max_total_bytes=100 * 1024 * 1024)
        await _wait_done(queue)
        await queue.close()

        assert len(message.albums_sent) == 1
        album = message.albums_sent[0]
        assert len(album) == 3
        # Caption must be escaped for HTML parse mode.
        assert "&amp;" in album[0].caption and "&lt;" in album[0].caption
        assert album[1].caption is None and album[2].caption is None
        statuses = {}
        async with db.sessions() as session:
            from db.database import Download
            from sqlalchemy import select
            for row in (await session.scalars(select(Download))).all():
                statuses[row.id] = row.status
        assert list(statuses.values()) == ["succeeded"]
        # Temporary directory is cleaned up by the pipeline; paths recorded
        # by the fetcher no longer exist after delivery.
        assert holder and not Path(holder[0]).exists()
        await db.close()
    asyncio.run(run())


def test_single_image_sends_photo(tmp_path):
    async def run():
        db = Database(f"sqlite+aiosqlite:///{tmp_path / 't.sqlite3'}")
        await db.initialize()
        queue = DownloadQueue(max_waiting=2)
        await queue.start()
        message = FakeMessage(12)
        query = FakeQuery(12, message)

        async def fetcher(url, directory, max_total_bytes):
            return FakeImageSet(directory, count=1)

        await start_image_download(query, url="https://www.instagram.com/p/ABC123xyz/",
                                   title="photo", platform="Instagram",
                                   fetcher=fetcher, queue=queue, db=db,
                                   max_total_bytes=100 * 1024 * 1024)
        await _wait_done(queue)
        await queue.close()
        assert len(message.photos_sent) == 1
        assert len(message.albums_sent) == 0
        await db.close()
    asyncio.run(run())


def test_image_fetch_failure_records_failed(tmp_path):
    async def run():
        db = Database(f"sqlite+aiosqlite:///{tmp_path / 't.sqlite3'}")
        await db.initialize()
        queue = DownloadQueue(max_waiting=2)
        await queue.start()
        message = FakeMessage(13)
        query = FakeQuery(13, message)

        async def failing_fetcher(url, directory, max_total_bytes):
            raise RuntimeError("blocked")

        await start_image_download(query, url="https://www.instagram.com/p/ABC123xyz/",
                                   title="photo", platform="Instagram",
                                   fetcher=failing_fetcher, queue=queue, db=db,
                                   max_total_bytes=100 * 1024 * 1024)
        await _wait_done(queue)
        await queue.close()
        status = message.children[-1]
        text, _ = status.edits[-1]
        assert "دریافت ناموفق بود" in text
        async with db.sessions() as session:
            from db.database import Download
            from sqlalchemy import select
            rows = (await session.scalars(select(Download))).all()
        assert [r.status for r in rows] == ["failed"]
        await db.close()
    asyncio.run(run())
