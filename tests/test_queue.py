import asyncio

import pytest

from workers.queue_manager import (
    DownloadQueue,
    DuplicateJobError,
    QueueFullError,
    UserBusyError,
    JobState,
)


async def _append(items, item):
    items.append(item)
    return item


def test_fifo_execution_and_duplicate_rejection():
    async def run():
        q = DownloadQueue(max_waiting=2)
        await q.start()
        seen = []
        job, pos = await q.submit("a", 1, "https://example.com/a", lambda: _append(seen, "a"))
        assert pos == 1
        with pytest.raises(DuplicateJobError):
            await q.submit("dup", 2, "https://example.com/a", lambda: _append(seen, "dup"))
        await q._queue.join()
        assert seen == ["a"]
        assert job.state == JobState.SUCCEEDED
        await q.close()
    asyncio.run(run())


def test_bounded_queue_full():
    async def run():
        q = DownloadQueue(max_waiting=2)
        # Worker is not started, so queue remains full for deterministic test.
        await q.submit("a", 1, "https://example.com/a", lambda: _append([], "a"))
        # Second submission from the *same* user is rejected as busy before
        # the full check can trigger, so a different user fills the queue.
        await q.submit("c", 2, "https://example.com/c", lambda: _append([], "c"))
        with pytest.raises(QueueFullError):
            await q.submit("b", 3, "https://example.com/b", lambda: _append([], "b"))
    asyncio.run(run())


def test_user_busy_rejected_at_submit():
    async def run():
        q = DownloadQueue(max_waiting=2, per_user_active=1)
        # First job stays QUEUED (no worker) — it must already block a second
        # submission from the same user.
        job, _ = await q.submit("a", 7, "https://example.com/a", lambda: _append([], "a"))
        assert job.state == JobState.QUEUED
        with pytest.raises(UserBusyError):
            await q.submit("b", 7, "https://example.com/b", lambda: _append([], "b"))
        # Other users are not affected.
        other, _ = await q.submit("c", 8, "https://example.com/c", lambda: _append([], "c"))
        assert other.state == JobState.QUEUED
    asyncio.run(run())


def test_cancel_queued_job_is_skipped_by_worker():
    async def run():
        q = DownloadQueue(max_waiting=2)
        await q.submit("a", 1, "https://example.com/a", lambda: _append([], "a"))
        assert await q.cancel("a", 1) is True
        assert q.get("a").state == JobState.CANCELLED
        await q.start()
        await q._queue.join()
        await q.close()
    asyncio.run(run())


def test_on_finish_called_for_success_and_failure():
    async def run():
        q = DownloadQueue(max_waiting=4)
        finished: list[tuple[str, str]] = []

        async def on_finish(job):
            finished.append((job.job_id, job.state.value))

        q.on_finish = on_finish
        await q.start()

        async def boom():
            raise RuntimeError("nope")

        await q.submit("ok", 1, "https://example.com/ok", lambda: _append([], "ok"))
        await q.submit("bad", 2, "https://example.com/bad", boom)
        await q._queue.join()
        states = dict(finished)
        assert states["ok"] == "succeeded"
        assert states["bad"] == "failed"
        await q.close()
    asyncio.run(run())


def test_finished_jobs_are_purged():
    async def run():
        q = DownloadQueue(max_waiting=4, keep_finished=1)
        await q.start()
        # Different users: the per-user limit would otherwise reject the
        # second and third submissions.
        for i in range(3):
            await q.submit(f"j{i}", i + 1, f"https://example.com/{i}", lambda: _append([], "x"))
        await q._queue.join()
        assert len(q._jobs) == 1
        await q.close()
    asyncio.run(run())


def test_cancel_running_job():
    async def run():
        q = DownloadQueue(max_waiting=2)
        release = asyncio.Event()

        async def wait_forever():
            await asyncio.wait_for(release.wait(), timeout=5)

        await q.start()
        await q.submit("a", 1, "https://example.com/a", wait_forever)
        await asyncio.sleep(0.05)  # let the worker pick it up
        assert q.snapshot()["active"] == 1
        assert await q.cancel("a", 1) is True
        await q._queue.join()
        job = q.get("a")
        assert job.state == JobState.CANCELLED
        release.set()
        await q.close()
    asyncio.run(run())
