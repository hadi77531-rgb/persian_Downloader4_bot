# Architecture — Persian Telegram Media Bot

**Phase:** 11 — hardening & integration (design originally approved in Phase 2)  
**Status:** Implemented through Phase 11; see "Current implementation state" at the end.  
**Last updated:** 2026-11-10

## 1. Product and policy boundaries

- Telegram UI and user-facing errors are Persian.
- The bot accepts only the supported public URL forms defined in configuration. It must not ask users for social-media passwords, browser cookies, or authentication tokens.
- Processing is limited to content that the requester owns or has permission to download and share. A public URL is not itself evidence of permission.
- Do not bypass login walls, privacy restrictions, DRM, rate limits, or other access controls. When a source blocks anonymous access, fail with a Persian explanation.
- Platform extraction is a replaceable capability, not a promise. Each extractor has independently tested capabilities and may report unsupported or temporarily unavailable.
- Initial delivery is direct Telegram upload only. No temporary public download links or paid/object storage in the initial architecture.

## 2. Deployment topology

### Initial target

A single Linux VM on an eligible zero-monthly-fee compute allocation (subject to account, capacity, and provider limitations), or an already-owned always-on server. Run one process/container that contains:

- Telegram long-polling bot process (aiogram 3.x)
- In-process bounded asynchronous job queue and worker task(s)
- SQLite database on persistent local disk
- Isolated temporary work directory on local disk
- yt-dlp and FFmpeg/ffprobe executables

This deliberately avoids Redis, managed PostgreSQL, external file storage, and a separate web service. A health-check endpoint is optional for future deployment and must not be treated as a substitute for the bot worker. Long polling is the initial transport because it is simpler for a single host and does not require a public webhook endpoint.

### Availability and durability

A single VM is a single point of failure. SQLite and temporary files are local; the operator must back up the database securely. Temporary media is never considered durable. Provider capacity, idle reclamation, availability, and free-tier terms can change. Do not claim an SLA or guaranteed zero future cost.

## 3. Proposed project boundaries

```text
telegram-downloader/
├── bot/
│   ├── handlers/          # start, help, URL intake, job controls, callbacks
│   ├── keyboards/         # Persian inline keyboards
│   ├── middlewares/       # user registration, throttling, error boundary
│   └── filters/           # admin and URL/platform filters
├── core/
│   ├── config.py          # Pydantic Settings / environment variables
│   ├── logging.py         # redacted structured logging
│   ├── security.py        # URL/host/IP/redirect validation
│   └── exceptions.py      # safe domain error types
├── db/
│   ├── models.py          # SQLAlchemy models
│   ├── database.py        # engine/session lifecycle
│   └── repositories.py    # bounded, parameterized DB access
├── services/
│   ├── contracts.py       # extraction and delivery interfaces
│   ├── youtube.py         # YouTube-specific capability adapter
│   ├── instagram.py       # anonymous public-content adapter
│   ├── pinterest.py       # public Pin adapter
│   ├── downloader.py      # shared yt-dlp orchestration and limits
│   └── media.py           # inspect/probe/optional safe conversion
├── workers/
│   ├── queue.py           # bounded queue, deduplication, cancellation
│   └── worker.py          # lifecycle, timeout, cleanup, status updates
├── admin/
│   ├── handlers.py        # admin-only Telegram callbacks/commands
│   └── statistics.py      # aggregate queries and health summary
├── utils/
├── tests/
│   ├── unit/              # deterministic mocked tests
│   └── integration/       # opt-in, authorized real public test URLs
├── downloads/             # runtime only; ignored by Git; private permissions
├── main.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
└── README.md
```

The tree is a proposal. Avoid creating empty service modules until their phase begins. Runtime download files must not be committed.

## 4. Service interfaces and ownership

### Intake / validation

`URLPolicy` parses with a standards-based URL parser, permits only `https`, rejects credentials embedded in URLs and nonstandard ports, matches an explicit domain allowlist, resolves DNS, and rejects loopback/private/link-local/multicast/reserved IP destinations. Every redirect is revalidated, with a small redirect cap. URL validation is performed before extraction and again for any resolved media URL or redirect where applicable. The HTTP client must not follow arbitrary redirects without this policy.

Validation only reduces SSRF risk; DNS rebinding and extractor-controlled requests remain concerns. Production implementation should run downloads in a restricted subprocess/container with no access to local services, credentials, or cloud metadata endpoints where feasible. Do not rely on hostname matching alone.

### Platform adapters

A common adapter contract should support:

- `supports(url) -> bool`
- `inspect(url) -> MediaDescriptor`
- `available_choices(descriptor) -> list[MediaChoice]`
- `download(url, choice, job_context) -> DownloadArtifact`

A `MediaDescriptor` contains only needed display fields (title, source/platform, media kind, duration, thumbnail if safely retrieved, and selectable formats). A `MediaChoice` identifies a validated format/quality or media group, not user-supplied command text. `DownloadArtifact` holds paths inside the job’s private temporary directory, detected media type, size, and metadata. Never serialize arbitrary extractor info to users or logs.

Platform adapters own platform-specific behavior and capability reporting. Shared orchestration owns global size/time limits, progress normalization, safe filenames, and cleanup. Do not use authenticated cookies. Unsupported or access-restricted cases return a typed safe error rather than trying alternate bypasses.

### Queue and worker

Use one process-local `asyncio.Queue` with configurable maximum queued jobs and one active job initially. Maintain a database job record for queued/running/terminal state. On restart, mark interrupted jobs failed/cancelled; do not blindly resume partial downloads. Duplicate handling uses a normalized URL fingerprint plus user/job state; do not persist query credentials or tokens. A user may cancel their own queued/running job; administrators may cancel any job. Cancellation must terminate the associated downloader process and run cleanup in `finally`.

Configure separately:

- active global jobs (initial default 1)
- queued jobs (bounded)
- per-user active/queued jobs
- per-user request window and cooldown
- extraction timeout and download timeout
- maximum bytes per media item and maximum aggregate bytes for a carousel
- maximum items in a carousel
- temporary-storage free-space threshold

All limits are environment/configuration defaults and may later be exposed as admin-controlled settings with safe bounds. Queue state is process-local and can be lost on restart; the DB is the durable record, not a distributed queue.

### Progress reporting

Normalize yt-dlp progress-hook data into percentage, bytes, speed, and ETA when those values exist. Throttle Telegram edits to avoid excessive API requests. Do not claim accurate percentage/ETA when source metadata does not provide enough information. Strip URLs, cookies, headers, and local paths from any displayed diagnostic.

### Delivery

Deliver an artifact only if type and size are within current Telegram Bot API limits. The reviewed public Bot API documentation states a 50 MB bot upload limit and a 20 MB bot download limit; verify again when implementing. Initial design uses conservative configurable upload threshold below the documented ceiling to leave room for API/client variability. If the file exceeds the threshold, explain that direct delivery is unavailable; do not silently upload to an unapproved third-party host. For carousel albums, split into valid Telegram media groups where possible, but enforce total size and item-count caps; if an individual item fails, report partial completion clearly.

### Temporary file lifecycle

Create a cryptographically unpredictable per-job directory beneath a configured root with restrictive permissions. Generate filenames from safe metadata plus random identifiers, never directly from a URL path. Ensure all resolved paths remain under the job directory. Use `TemporaryDirectory`-style lifecycle management and unconditional cleanup on success, failure, timeout, cancellation, and shutdown. A periodic startup/maintenance sweep may remove only stale directories matching the bot’s own naming pattern. The cleanup age and maximum disk use are configurable. Keep no temporary link after a job.

## 5. Database design

Use SQLAlchemy with SQLite, foreign keys enabled, transactions for state transitions, and indexes on commonly queried columns. SQLite is appropriate for one writer-oriented bot process at modest scale; it is not a multi-host database or a substitute for backups.

### Tables

**users**
- `telegram_id` (primary key; integer)
- `username` (nullable, current value)
- `first_name` (nullable, sanitized display value)
- `registered_at`, `last_activity_at` (UTC timestamps)
- `status` (active/blocked)
- `blocked_at`, `blocked_by` (nullable)

**downloads**
- `id` (UUID/string primary key)
- `user_id` (FK to users)
- `platform` (enum/string)
- `source_url` (sanitized/canonical form; strip sensitive query values)
- `url_fingerprint` (non-reversible digest for deduplication)
- `media_type`, `selected_quality` (nullable where irrelevant)
- `file_size_bytes` (nullable)
- `status` (queued/inspecting/downloading/delivering/succeeded/failed/cancelled)
- `error_code` (safe internal category, not stack trace)
- `created_at`, `started_at`, `completed_at`

**settings**
- `key` (primary key), typed JSON/text value, `updated_at`, `updated_by`
- Only allowlisted setting names and validated value ranges may be changed by an admin.

**Optional daily aggregates**
- `date`, `platform`, `status`, `count` with a composite uniqueness/index. Start with indexed aggregate queries against downloads; introduce rollups only if query volume or retention makes it useful.

Keep metadata minimal. Do not store downloaded content, credentials, cookies, or full extractor payloads. Define a retention policy for download history and error records before public launch; support configurable pruning so SQLite cannot grow indefinitely.

## 6. Telegram workflow and UI states

1. `/start`: Persian welcome, platform buttons, help/about.
2. User sends a URL. Register/update user; check blocked status and rate limits.
3. Validate syntax, scheme, hostname, DNS/IP, redirects, then identify supported platform.
4. Create a job row and enqueue if capacity allows; otherwise give Persian queue-full or queue-position response.
5. Inspect media without downloading the whole file. Return accurate choices only. For platforms/formats where choices cannot be discovered reliably, offer a safe best-available option or explain unavailability.
6. User chooses an inline option. Callback data contains only an opaque job ID and choice key; validate user ownership, job state, expiry, and choice against stored server-side data.
7. Worker downloads under limits, updates progress and status, and supports cancellation.
8. Verify actual file type, size, and path; deliver directly to Telegram.
9. Record outcome and clean temporary files regardless of outcome.

Inline callbacks are untrusted input. Never let callback data supply a URL, file path, raw yt-dlp format expression, or shell arguments.

## 7. Admin authorization and controls

- `ADMIN_IDS` is a required environment setting for admin functions; compare Telegram numeric IDs on every admin action.
- No user-visible admin menu or sensitive data for non-admin users. Unauthorized callbacks receive a generic denial.
- Admin features: aggregate counts, recent safe error categories, queue/active state, system health (disk free, process uptime, memory/CPU when available), user block/unblock, and bounded configuration changes.
- Do not include tokens, full source URLs with sensitive query strings, local paths, or stack traces in Telegram admin output.
- “Restart worker” is not an arbitrary remote shell operation. Initial deployment should use process/container orchestration for restart; any future admin restart control must use a narrowly scoped, audited mechanism.

## 8. Configuration and secrets

Settings come from environment variables via Pydantic Settings. `.env` is for local development only and excluded from Git. Required secrets include Telegram bot token and admin IDs. Set conservative defaults for concurrency, queue size, file size, timeouts, rate limit, storage path, and retention. Validate configuration on startup and fail closed for missing bot token or unsafe values. Never print the environment or secrets in logs.

## 9. Testing strategy for later phases

- Unit tests: URL parser/domain allowlist, IP/redirect rejection, platform detection, quality mapping, state transitions, callback ownership, admin access, rate limits, path containment, cleanup, limits, and error translation.
- Mocked extractor tests: metadata/format edge cases, progress, cancellation, timeout, malformed extractor output, and simulated platform failures. These validate our integration logic only, not real platform availability.
- Integration tests: opt-in and run separately with authorized, stable public test media per platform. Record date, tested URL class, yt-dlp version, and result. Never run private/login-required content tests.
- Delivery tests: mocked Telegram API tests for threshold handling and album behavior; no assertion of real Telegram acceptance without an actual integration run.
- Security tests: SSRF cases including IPv4/IPv6 private ranges, DNS resolution/redirect changes, path traversal, oversized metadata, malicious filenames, callback forgery, and log redaction.

## 10. Open decisions before or during implementation

1. Confirm whether OCI account creation requiring a payment card is acceptable; if not, target existing hardware or a genuinely no-card host only for a prototype.
2. Confirm conservative default maximum media size and carousel aggregate size. Initial proposed policy: keep the configured direct-send cap below Telegram’s documented maximum, and reject larger jobs rather than provide links.
3. Confirm retention period for download history and blocked-user records.
4. Confirm the bot’s policy text for user rights and authorized downloads, especially for YouTube.
5. Confirm whether to implement only source-supported anonymous public extraction, with all access-restricted URLs rejected.

## 11. Architecture risks

- Platform extractor behavior is unstable and can break without notice.
- Public availability does not establish download permission; platform terms and copyright law remain applicable.
- Single VM, local database, and process-local queue have no high availability and limited recovery behavior.
- Free-tier capacity, limits, account eligibility, and provider policies may change.
- Telegram file-size/API limits may change; verify before coding delivery.
- CPU, disk, and network use scale with user-submitted media and abuse; hard caps and rate limits are required for any public deployment.

## 12. Current implementation state (Phase 11, 2026-11-10)

How the code maps onto this design today:

- **Implemented:** Persian aiogram 3 long-polling bot; `core/security.py` URL policy (HTTPS-only, registrable-domain allowlist, DNS public-IP preflight, path containment); `services/{youtube,instagram,pinterest}.py` adapters running yt-dlp in isolated `python -m yt_dlp` subprocesses (own process group, minimal env, hard timeouts, killpg on cancel/timeout); `workers/queue_manager.py` bounded queue with deduplication, per-user active limit, ownership-checked cancellation, finished-job purge and an `on_finish` hook; `bot/handlers/common.py` shared download pipeline (queue submission, progress rendering, cancel button, full DB lifecycle recording); `bot/middlewares/block_check.py` blocked-user enforcement; rate limiting; SQLite users/downloads with retention pruning; admin dashboard with platform breakdown and live queue snapshot; Docker/Compose/CI files.
- **Deliberate deviations from the proposal:** single flat modules instead of the proposed `contracts.py`/`repositories.py`/`filters/` split (the project stayed small); redirects are NOT revalidated per-hop (yt-dlp internals are opaque to us — covered by the documented egress-sandbox blocker); no `settings` table yet (limits come from env only); `blocked_at`/`blocked_by` columns not implemented; carousel support not implemented, so `MAX_CAROUSEL_MB` is currently unused.
- **Still open (public-release blockers):** network egress sandbox / redirect-aware SSRF control; strict disk quotas; DB backups; dependency pinning; live authorized extraction tests per platform; independent security review.
- **Live probe (2026-11-10, datacenter IP):** anonymous YouTube metadata inspection is rejected by YouTube's bot-check ("Sign in to confirm you're not a bot"). This matches the documented best-effort policy: the bot never uses cookies/credentials, so datacenter-hosted deployments must expect anonymous extraction to fail on some platforms; the failure surfaces as a safe Persian error.
