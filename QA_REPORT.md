# Final QA Report — Phase 11

**Date:** 2026-11-10  
**Assessment:** Development prototype; **not release-ready for public users**.

## Executed checks

| Check | Result | Scope / caveat |
|---|---|---|
| `python -m pytest -q` | **46 passed** | Unit and mocked tests only. Includes URL security logic, queue behavior (FIFO, duplicate, busy, cancel, purge, on-finish), platform choice filtering, SQLite records, retention pruning, and pending-store TTL logic. |
| `python -m compileall -q main.py core bot db services workers admin tests` | **Passed** | Syntax/bytecode compilation; not runtime end-to-end validation. |
| Module import check (without `BOT_TOKEN`) | **Passed** | Handlers no longer read settings at import time; the bot fails fast only at startup when the token is missing. |
| `python -m pip check` | **Passed** — no broken requirements | Installed environment dependency consistency only. |
| `MAX_FILE_MB=50` startup-crash regression | **Fixed and tested** | Values above the 49 MB upload ceiling are clamped instead of failing validation. |

## Phase 11 changes (this revision)

1. **Queue integrated end-to-end.** All platform downloads now run through the bounded `DownloadQueue`; handlers can no longer bypass it. Users see queue position, a cancel button, and explicit messages for duplicate/busy/full queue states.
2. **Blocked-user enforcement everywhere** via middleware (previously only one text handler checked it).
3. **Complete download event recording** (queued/running/terminal states) — admin statistics now reflect real activity, including a 24-hour platform breakdown and live queue snapshot.
4. **Retention pruning** of download records per `HISTORY_RETENTION_DAYS`.
5. **Process-group kill** for yt-dlp subprocesses (FFmpeg descendants no longer survive cancellation on Linux).
6. **Memory-bounded stores**: queue registry purge, TTL'd pending selections, rate-limit bucket pruning.
7. **Domain allowlist improvement**: legitimate platform subdomains accepted; raw-IP hosts and anchored-suffix lookalikes rejected.
8. **Startup-crash fixes**: `MAX_FILE_MB=50` no longer aborts startup (clamped to 49); handlers import without a configured `.env`.
9. **Dead code removal** (unused synchronous yt-dlp path), reducing attack surface.
10. **Configurable file-size cap** wired from settings into the downloader (`MAX_FILE_MB` is now effective, previously hardcoded).

## Not run / unverified

- Docker image build, Compose validation, or container runtime test: Docker CLI is unavailable in this environment.
- Telegram end-to-end test: no bot token configured; no real Telegram account interaction performed.
- FFmpeg runtime/MP3 conversion: FFmpeg was absent from this environment.
- Resource load, bandwidth, storage quota, cancellation behavior under load, vulnerability scan, external penetration test, dependency-license audit, and real host deployment: not run.

## Live platform probe (2026-11-10)

A live anonymous metadata inspection of a public YouTube video (`jNQXAC9IVRw`) was attempted from this datacenter environment. **Result: rejected by YouTube's bot-check** ("Sign in to confirm you're not a bot"). This is the documented, expected behavior for cookie-free anonymous extraction from datacenter IPs — not a regression of this codebase. Verification outcomes:

- The subprocess pipeline, exit-code handling, and safe Persian error path behave correctly.
- Anonymous extraction success from residential IPs remains **unverified**; Instagram and Pinterest remain **untested live**.
- Datacenter-hosted deployments must expect bot-check failures on some platforms; the bot intentionally never uses cookies or credentials.

## Outstanding product and security gaps

1. Instagram image posts/carousels and Pinterest image Pins are unsupported.
2. Instagram and Pinterest video extractors may be unavailable or change; no live tests exist.
3. YouTube downloads are subject to YouTube terms and user rights; technical availability is not permission.
4. SSRF remains incompletely controlled: DNS preflight does not prevent rebinding or validate all redirects/CDN requests; no egress network sandbox exists.
5. The rate limiter and queue are process-local; multi-instance deployments need external state.
6. Retention covers downloads only; user rows, backups, strict disk quotas, and deletion-request workflows remain incomplete.
7. Docker/Compose files are unvalidated in a real container environment; healthcheck only checks process liveness.
8. No persistent free-hosting provider or public deployment has been verified.

## Release recommendation

**Do not publish this bot for public use yet.** Before release, at minimum: add strict disk/process/network isolation and redirect-aware SSRF protection; validate the container; run live authorized test cases for each supported platform and Telegram delivery; and obtain an independent security review. The previous blockers — queue integration, blocked-user enforcement, and event recording — are resolved in this revision.

---

# Revision 2 — Bug-fix & Instagram images/carousels (2026-11-11)

Independent review findings fixed in this revision; full suite now **83 passed** (`python -m pytest -q`).

## Fixes

1. **HTML injection / parse failures (high).** Media titles reached Telegram raw under the default HTML parse mode; titles containing `&`, `<`, `>` (very common) broke quality menus and blocked file delivery. All render sites now escape titles (`bot/handlers/youtube.py`, `instagram.py`, `pinterest.py`, `bot/handlers/common.py` caption/album caption). Regression tests included.
2. **Queue shutdown hang (high, latent).** `_execute`'s `uncancel()` unconditionally swallowed worker cancellation, so an externally cancelled worker (e.g. event-loop shutdown while a job was running) survived its own cancellation and hung `asyncio.run()`. The worker now only swallows cancellation during its own `close()` and honours external cancellation. Reproduced by `test_finished_jobs_are_purged` before the fix.
3. **Per-user limit now counts queued jobs (medium).** Previously only *running* jobs were counted, letting users stack several queued requests, contradicting the advertised one-active-download rule. `submit()` now counts all unfinished (queued+running) jobs per user.
4. **Misleading quality buttons (low).** Videos whose every available height exceeded a preset cap (e.g. 4K-only) produced three buttons all labelled "تا 2160p" while the selectors capped lower. Capped options are now only offered when something at-or-below the cap exists; otherwise a single honest "بهترین کیفیت" option is shown. Choice keys are guaranteed unique.
5. **yt-dlp `null` payload guard (low).** `inspect()` now rejects non-object JSON via `_parse_info()` (yt-dlp can exit 0 while printing `null`).
6. **Unused `MAX_CAROUSEL_MB`** is now wired: it is the aggregate cap for Instagram image/carousel downloads.
7. **`tests/test_process_kill.py`** uses `sys.executable` instead of assuming `python` exists.

## New feature — Instagram image posts & photo carousels

- `services/instagram.py`: metadata via the public web API (`/api/v1/media/{id}/info/` with the well-known public `x-ig-app-id` header, no login/cookies), shortcode→media-id conversion, up to 10 images per request.
- CDN hardening: HTTPS-only, official CDN domains only (`cdninstagram.com`, `fbcdn.net`), DNS preflight for public addresses, per-image cap (10 MB) and aggregate cap (`MAX_CAROUSEL_MB`), content-type allowlist.
- Delivery: single photo or media-group album (≤10 items) with escaped caption; flows through the same queue, cancel button and DB event recording (`media_type=image`).
- Handler flow: video first (yt-dlp); if unavailable, image/carousel branch; graceful Persian failure texts when Instagram blocks anonymous access (expected on datacenter IPs such as Railway/Back4App).
- Tests: shortcode conversion, candidate/CDN filtering, total-cap abort, carousel metadata parsing, album/photo delivery pipeline, DB lifecycle.

## Still unverified

- No live Telegram E2E and no live Instagram/Pinterest extraction tests (no token in CI; datacenter IPs). Pinterest image Pins remain unsupported. SSRF rebinding/redirect gap and multi-instance limits unchanged (see Revision 1 notes).
