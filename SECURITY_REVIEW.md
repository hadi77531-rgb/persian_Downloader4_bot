# Security Review — Phase 11

**Review date:** 2026-11-10  
**Scope:** Current repository implementation; static review and unit tests only. No penetration test or live-platform test was run.

## Improvements made (cumulative through Phase 11)

- HTTPS allowlist restricted to registrable source domains (exact match or anchored subdomain suffix), which now also accepts legitimate platform subdomains such as `m.youtube.com`, `music.youtube.com`, and country subdomains of `pinterest.com` while still rejecting lookalikes (`youtube.com.evil.com`, `evil-youtube.com`), credentials in URLs, unsupported ports/schemes, whitespace, overlong URLs, and raw IP hosts.
- DNS preflight rejects all resolved IPs that are not globally routable.
- Resolved-path containment check for output files (`ensure_child_path`).
- yt-dlp inspect and download run in external `python -m yt_dlp` subprocesses with argument arrays (no shell), private temporary HOME/config, `--ignore-config`, no cookies, no playlist, bounded socket/retry settings, hard metadata/download timeouts, and `start_new_session=True` so timeout/cancellation terminates the **entire process group** (yt-dlp and descendant FFmpeg) via `killpg`.
- **All downloads flow through the bounded queue** (one active job per user, bounded waiting room, duplicate URL rejection, per-user busy rejection at submit time) — handlers no longer start downloads outside the queue.
- **Blocked-user enforcement across every router** via `BlockCheckMiddleware` (DB-backed, 60s TTL cache, admins exempt, fail-open with logging on DB errors).
- **Download lifecycle recording**: queued → running → succeeded/failed/cancelled written to SQLite for every job (including queue rejections and worker-observed cancellations via the `on_finish` hook).
- Retention pruning: download records older than `HISTORY_RETENTION_DAYS` are deleted at startup and every 6 hours.
- Bounded process-local stores: finished jobs are purged from the queue registry; pending quality selections expire by TTL; rate-limit buckets are pruned.
- Output filename restriction, trusted server-side format choices, output size validation (49 MiB effective cap, config values above the cap are clamped instead of crashing startup), rate limiting, admin-ID checks, and generic user-facing errors remain in place.

## Tests executed

`python -m pytest -q` — **46 passed**. Python `compileall` passed. Tests include URL allowlist (subdomains + lookalikes), DNS private-address rejection with mocked resolver, path containment, platform choice filtering, queue FIFO/duplicate/full/busy/cancel/purge/on-finish behavior, SQLite user/download records, retention pruning, platform breakdown, and pending-store TTL/bounding.

## Remaining risks / not yet security-approved for public deployment

1. **SSRF is not fully eliminated.** DNS is checked before extraction, but DNS rebinding between preflight and connection, and redirects/CDN/API requests made internally by yt-dlp, are not individually pinned or revalidated. The subprocess is not network-sandboxed. For a public service, run workers in an isolated container/VM with egress firewalling, no cloud metadata/local-service access, and a reviewed destination policy, or implement a downloader network layer that validates every connection and redirect.
2. **Format options are internal constants.** Maintain that invariant: never accept raw yt-dlp selectors, output templates, file paths, or process flags from Telegram input.
3. **Downloads can still consume resources.** The file-size option and post-download check are not a strict filesystem quota; tmpfs/`/tmp` capacity bounds actual usage (512 MB in Compose). Add OS/container disk and CPU/memory limits appropriate to the host before public launch.
4. **Rate limiter and queue are process-local.** Both are intentional for a single-process bot; multi-instance deployments would need external state (Redis/Postgres).
5. **Telegram token and logs.** Tokens are environment-backed and not intentionally logged; run a separate log-redaction review and restrict access to process environment, host, and deployment logs.
6. **Block cache lag.** A blocked user may continue acting for up to 60 seconds (cache TTL); lower the TTL or invalidate on admin action if this matters operationally.
7. **Storage and privacy.** Retention pruning covers downloads; user rows are kept (needed for block enforcement). Database backups, file permissions, and deletion-request workflows remain unimplemented.
8. **Supply chain.** Dependencies are version ranges rather than a fully hash-pinned lockfile. Pin and audit dependencies for reproducible production builds; keep yt-dlp updated carefully because extractor changes are frequent.
9. **No live test or external audit.** This report is not a certification. Do not expose the bot publicly until the above high-risk gaps are addressed and reviewed.

## Deployment gate

Treat the current project as development-only. Minimum pre-publication blockers: network-isolated worker/SSRF mitigation; strict disk/resource quotas; database backups; dependency pinning; live authorized test media per platform; and a separate security/QA review. Queue integration and blocked-user enforcement — previous blockers — are now implemented and tested.
