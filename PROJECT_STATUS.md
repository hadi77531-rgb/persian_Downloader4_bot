# Project Status

**Project:** Persian Telegram Media Downloader Bot  
**Updated:** 2026-11-11  
**Current phase:** Phase 12 — independent bug-fix release (HTML escaping, queue shutdown hang, per-user queued limit, honest quality labels) + Instagram image/carousel support. Awaiting live authorized testing (see `QA_REPORT.md` Revision 2).

## User-approved decisions

- Hosting assumption: existing hardware / no-card options only; do not assume OCI eligibility.
- Initial limits: 49 MB effective per media file (configurable via `MAX_FILE_MB`, clamped to the Telegram upload ceiling) and 100 MB aggregate per carousel; 30-day history retention.
- Policy: authorized-use only; no private or access-controlled content, credentials, or bypasses.

## Phase 1 — Research and feasibility

Completed. Best-effort zero-monthly-cost candidate is existing always-on hardware or an eligible free VM. Free PaaS options reviewed are not a reliable match for an always-on public download worker. YouTube extraction has significant platform-terms constraints; Instagram/Pinterest support must be independently verified.

## Phase 2 — Architecture

Completed in `ARCHITECTURE.md`.

## Phase 3 — Telegram core

Completed: Persian UI, supported-domain URL validation, platform detection, SQLite user registration, environment config. Initial tests passed (15).

## Phase 4 — YouTube

- Implemented anonymous yt-dlp metadata inspection and format choices for available 1080p/720p/480p-or-best formats, plus MP3 when an audio stream is present.
- Implemented quality selection callbacks with ownership checks, download progress (when yt-dlp provides totals), best-effort direct Telegram delivery, and temporary-directory cleanup.
- yt-dlp is configured without credentials/cookies and with no-playlist, socket timeout, retries, filename restrictions, and a 50 MiB size cap. MP3 conversion requires FFmpeg.
- Automated tests: `python -m pytest -q` — **17 passed**. `compileall` passed.
- **No real YouTube integration test was run.** YouTube download success and format options have not been verified against a live authorized sample. FFmpeg is not installed in this environment (`ffmpeg: command not found`), so MP3 conversion is unavailable here.
- Known gap: yt-dlp runs via a Python thread. Coroutine cancellation cannot reliably terminate that thread; hard download timeout/cancellation requires a killable isolated subprocess and is deferred to security/worker hardening. This current build is not ready for public deployment.
- YouTube Terms and user rights remain applicable. Only use for content the user is authorized to download; this code does not imply all YouTube downloads are permitted.

## Phase 5 — Instagram

- Implemented a best-effort anonymous Instagram video adapter reusing yt-dlp, quality selection, progress, ownership-checked selection callbacks, direct Telegram delivery, and temporary directory cleanup.
- Instagram MP3 is intentionally not offered.
- **Public photo and mixed/image carousel downloading is not implemented.** Upstream yt-dlp discussion states yt-dlp is not an image downloader; implementing that would require separately evaluating another tool and ensuring it works without authentication, which is not established here.
- Login/cookie workflows are not configured; if Instagram requires authentication or blocks anonymous access, the user receives a safe failure response.
- Automated tests: `python -m pytest -q` — **19 passed**; compilation passed.
- **No real Instagram integration test was run.** Anonymous reel/video extraction is not verified against a live authorized URL. Instagram support remains conditional.

## Phase 6 — Pinterest

- Implemented a best-effort anonymous **video Pin** adapter using yt-dlp, with quality selection, progress callbacks, ownership checks, direct Telegram delivery, and temporary-file cleanup.
- Still-image Pins are not implemented. The adapter rejects audio-only options and does not advertise image support.
- Pinterest extractor reliability is not guaranteed. Upstream issue #13554 reports public video Pins failing with HTTP 404; this is evidence of instability, not proof every current URL fails.
- Automated tests: `python -m pytest -q` — **21 passed**; compilation passed.
- **No real Pinterest integration test was run.** Video extraction is unverified against a live authorized URL; do not promise operational support.

## Phase 7 — Advanced features (partial implementation)

- Added a bounded FIFO in-process async queue manager with URL deduplication, queue-full errors, state tracking, cancellation APIs, and one active worker.
- Added in-memory sliding-window rate-limit middleware (6 events per 60 seconds by default; admins exempt).
- Added SQLAlchemy downloads table, status-recording method, user list/block methods, and basic aggregate totals.
- Added Persian /admin dashboard with basic counts, recent users, block/unblock controls, and disk free-space health check.
- Tests: `python -m pytest -q` — **23 passed**; compilation passed.
- **Not yet wired end-to-end:** current platform download handlers still execute downloads directly rather than submitting them to the queue; download events are not yet consistently recorded, so dashboard download totals remain incomplete. Queue cancellation cannot reliably stop yt-dlp's thread. Per-platform/daily/weekly/monthly breakdowns, recent errors, live active/queue stats, limits configuration and worker restart are not implemented. User block checks are not yet enforced across every router. Do not treat this phase as complete for public deployment.

## Phase 8 — Security and optimization (partial; deployment gate remains closed)

- Tightened exact source-host validation; added DNS public-IP preflight and path containment checks.
- Switched yt-dlp inspect/download operations to `python -m yt_dlp` subprocesses with argument arrays, isolated temporary HOME/config, no inherited secrets/cookies, timeouts and process termination on cancellation; set output size checks and generic user errors.
- Added `SECURITY_REVIEW.md` with tests, remaining risks, and explicit deployment blockers.
- Tests: `python -m pytest -q` — **26 passed**; Python compilation passed.
- Critical remaining limitation: DNS rebinding and yt-dlp internal redirects/CDN requests are not controlled by a network egress sandbox; no strict filesystem quota; queue and cancellation not end-to-end; blocked-user enforcement and retention not complete. This is not yet approved for public deployment.

## Phase 9 — Deployment preparation

- Added Dockerfile (Python 3.12, FFmpeg, non-root runtime), Compose configuration (persistent SQLite volume, resource limits, read-only root, temporary tmpfs, restart policy), and `.dockerignore`.
- Added Persian self-hosting guide `DEPLOYMENT_FA.md` for no-new-hosting-bill existing hardware, secrets, run/stop, storage, resource constraints, and troubleshooting.
- Added GitHub Actions test workflow for Python 3.12. This runs tests only; it does not auto-deploy or publish an image.
- QA: `python -m pytest -q` — **26 passed**; compilation passed. Docker CLI is unavailable in this environment, so image build and Compose validation were not run.
- No external deployment, GitHub repository, or host was configured. This is deployment preparation, not a live deployment. Public-release security blockers from `SECURITY_REVIEW.md` remain.

## Phase 10 — Final QA

- Completed test run: **27 passed**; Python compileall passed; `pip check` found no broken requirements.
- Static credential-pattern scan found no token-shaped credential; `.env.example` is placeholder-only. This was not a history audit or full secrets scan.
- Added `QA_REPORT.md` with test coverage, unverified checks, known gaps, risks, and release recommendation.
- Docker build/Compose validation not run (Docker unavailable). No bot token, Telegram E2E test, live media extraction test, or real deployment was available.
- Final recommendation: **development prototype only; not ready for public release** pending the blockers in `SECURITY_REVIEW.md` and `QA_REPORT.md`.

## Files added/modified in Phase 4

- `services/youtube.py`
- `bot/handlers/youtube.py`
- `main.py` (router wiring)
- `requirements.txt` (yt-dlp)
- `tests/test_youtube.py`
- `PROJECT_STATUS.md`

## Phase 11 — Hardening & integration (2026-11-10)

- **Queue wired end-to-end:** every platform download (YouTube/Instagram/Pinterest) is submitted to the bounded `DownloadQueue`; duplicate, per-user-busy, and full-queue states produce explicit Persian feedback. A cancel button lets the owner stop queued or running jobs.
- **Shared download pipeline** (`bot/handlers/common.py`): replaces ~240 duplicated lines across the three platform handlers; owns progress rendering, DB event recording, and cancellation UX.
- **Blocked-user enforcement across all routers** via `BlockCheckMiddleware` (DB-backed, 60s TTL cache, admins exempt, fail-open on DB errors).
- **Download lifecycle recorded** in SQLite: queued → running → succeeded/failed/cancelled, including queue rejections and worker-observed cancellations (queue `on_finish` hook). Admin dashboard gained a 24-hour platform breakdown and live queue snapshot.
- **Retention pruning:** download records older than `HISTORY_RETENTION_DAYS` are deleted at startup and every 6 hours.
- **Process-group kill:** yt-dlp subprocesses start with `start_new_session=True`; timeouts/cancellation kill the whole group so FFmpeg descendants no longer survive.
- **Memory bounds:** finished jobs purged from the queue registry (`keep_finished`), pending quality selections TTL-expiring (`PendingStore`), rate-limit buckets pruned.
- **Domain allowlist:** registrable-domain suffix matching (accepts `m.youtube.com`, `music.youtube.com`, country Pinterest subdomains) while rejecting raw-IP hosts and anchored-suffix lookalikes.
- **Startup robustness:** handlers no longer read settings at import time; `MAX_FILE_MB` above the upload ceiling is clamped instead of crashing; the effective file cap (49 MB) is now driven by configuration end-to-end.
- **Dead code removed:** unused synchronous yt-dlp inspection/download path.
- Tests: `python -m pytest -q` — **46 passed**; `compileall` passed; module imports verified without a configured `.env`.
- Still outstanding: no live platform tests, no Docker build validation, SSRF/egress sandbox remains a public-release blocker (see `SECURITY_REVIEW.md`).
