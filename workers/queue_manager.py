"""Bounded single-process FIFO work queue for async download jobs."""
import asyncio
import contextlib
import hashlib
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Awaitable, Callable, Any
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL_STATES = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED})


@dataclass
class Job:
    job_id: str
    user_id: int
    url_key: str
    work: Callable[[], Awaitable[Any]] = field(repr=False)
    state: JobState = JobState.QUEUED
    task: asyncio.Task | None = field(default=None, repr=False)
    result: Any = field(default=None, repr=False)
    error: Exception | None = field(default=None, repr=False)
    meta: dict[str, Any] = field(default_factory=dict)


def fingerprint_url(url: str) -> str:
    parts = urlsplit(url.strip())
    normalized = urlunsplit((parts.scheme.lower(), (parts.hostname or "").lower(), parts.path.rstrip("/"), parts.query, ""))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class QueueFullError(RuntimeError):
    pass


class DuplicateJobError(RuntimeError):
    pass


class UserBusyError(RuntimeError):
    pass


class DownloadQueue:
    """Bounded waiting room, per-user limit, ownership-aware cancel.

    Work closures must be safe to run in the event loop (typically they await
    asyncio.to_thread or spawn isolated subprocesses). This queue is in-memory
    and intentionally single-process; it is not durable across a restart.

    The per-user limit counts *unfinished* jobs — both QUEUED and RUNNING —
    so a user cannot stack several requests while their first one waits in
    the queue (``_active_by_user`` only tracks currently-running jobs and is
    used for execution-time accounting).

    ``on_finish`` (if set) is awaited with the finished Job for every terminal
    outcome — including jobs skipped by the per-user limit and cancelled jobs —
    so callers can record durable state without coupling the queue to storage.
    """
    def __init__(self, max_waiting: int = 20, per_user_active: int = 1, keep_finished: int = 100):
        if max_waiting < 1 or per_user_active < 1 or keep_finished < 1:
            raise ValueError("queue limits must be positive")
        self.max_waiting = max_waiting
        self.per_user_active = per_user_active
        self.keep_finished = keep_finished
        self.on_finish: Callable[[Job], Awaitable[None]] | None = None
        self._queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=max_waiting)
        self._jobs: dict[str, Job] = {}
        self._active_by_user: dict[int, int] = {}
        self._worker: asyncio.Task | None = None
        self._closing = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="download-queue-worker")

    async def submit(self, job_id: str, user_id: int, url: str, work: Callable[[], Awaitable[Any]],
                     meta: dict[str, Any] | None = None) -> tuple[Job, int]:
        async with self._lock:
            if self._closing:
                raise QueueFullError("queue is shutting down")
            key = fingerprint_url(url)
            if any(j.url_key == key and j.state in (JobState.QUEUED, JobState.RUNNING) for j in self._jobs.values()):
                raise DuplicateJobError("same URL is already queued or running")
            # Count every unfinished job of this user (queued *and* running),
            # otherwise users could stack queued jobs behind other users' work.
            unfinished = sum(
                1 for j in self._jobs.values()
                if j.user_id == user_id and j.state in (JobState.QUEUED, JobState.RUNNING)
            )
            if unfinished >= self.per_user_active:
                raise UserBusyError("user already has an active download")
            if self._queue.full():
                raise QueueFullError("queue is full")
            job = Job(job_id, user_id, key, work, meta=meta or {})
            self._jobs[job_id] = job
            await self._queue.put(job)
            return job, self._queue.qsize()

    async def _run(self) -> None:
        while True:
            if self._closing and self._queue.empty():
                return
            try:
                job = await asyncio.wait_for(self._queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            try:
                await self._execute(job)
            except Exception:
                logger.exception("queue worker internal error")
            finally:
                self._queue.task_done()

    async def _execute(self, job: Job) -> None:
        try:
            if job.state == JobState.CANCELLED:
                return
            count = self._active_by_user.get(job.user_id, 0)
            if count >= self.per_user_active:
                job.state = JobState.FAILED
                job.error = RuntimeError("per-user concurrency limit")
                return
            self._active_by_user[job.user_id] = count + 1
            job.state = JobState.RUNNING
            job.task = asyncio.create_task(job.work(), name=f"download-{job.job_id}")
            job.result = await job.task
            job.state = JobState.SUCCEEDED
        except asyncio.CancelledError:
            job.state = JobState.CANCELLED
            if job.task and not job.task.done():
                job.task.cancel()
            current = asyncio.current_task()
            worker_cancelled = current is not None and current.cancelling() > 0
            if worker_cancelled and self._closing:
                # Our own close() cancelled the worker: clear the pending
                # cancellation so cleanup awaits below can still run; the
                # worker then exits via the closing check in _run().
                current.uncancel()
            elif worker_cancelled:
                # External cancellation (e.g. event-loop shutdown): run the
                # cleanup in ``finally`` but honour the cancellation instead
                # of surviving it, otherwise asyncio.run() would hang waiting
                # for an unkillable worker task.
                raise
            # else: the CancelledError came from cancelling ``job.task``; the
            # worker itself is fine and simply continues with the next job.
        except Exception as exc:
            job.error = exc
            job.state = JobState.FAILED
        finally:
            if self._active_by_user.get(job.user_id):
                self._active_by_user[job.user_id] -= 1
            if job.state in _TERMINAL_STATES and self.on_finish is not None:
                try:
                    await self.on_finish(job)
                except Exception:
                    logger.exception("on_finish callback failed for job %s", job.job_id)
            self._purge_finished()

    def _purge_finished(self) -> None:
        finished = [jid for jid, j in self._jobs.items() if j.state in _TERMINAL_STATES]
        excess = len(finished) - self.keep_finished
        for jid in finished[:max(0, excess)]:
            self._jobs.pop(jid, None)

    async def cancel(self, job_id: str, requester_id: int, is_admin: bool = False) -> bool:
        job = self._jobs.get(job_id)
        if job is None or (job.user_id != requester_id and not is_admin):
            return False
        if job.state == JobState.QUEUED:
            # asyncio.Queue does not support removal; mark and worker will skip it.
            job.state = JobState.CANCELLED
            return True
        if job.state == JobState.RUNNING and job.task:
            job.task.cancel()
            return True
        return False

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def snapshot(self) -> dict[str, int]:
        return {
            "queued": sum(j.state == JobState.QUEUED for j in self._jobs.values()),
            "active": sum(j.state == JobState.RUNNING for j in self._jobs.values()),
            "total": len(self._jobs),
        }

    async def close(self, drain: bool = False) -> None:
        self._closing = True
        if self._worker is None:
            return
        if drain:
            await self._queue.join()
        else:
            self._worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._worker
        self._worker = None
