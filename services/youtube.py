"""Anonymous, best-effort YouTube extraction via yt-dlp.

Use only for media the requester is authorized to download. No cookies or
credential-bearing options are configured. Requires ffmpeg for MP3 conversion.

Extraction runs in isolated ``python -m yt_dlp`` subprocesses started in their
own process group, so cancellation and timeouts can terminate the downloader
*and* any descendant (e.g. ffmpeg) via killpg.
"""
import asyncio
import json
import os
import re
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.security import validate_and_detect_url, validate_public_destination, ensure_child_path

DEFAULT_MAX_BYTES = 49 * 1024 * 1024  # safety margin under the 50 MB Bot API upload ceiling
_INSPECT_TIMEOUT = 45
_DOWNLOAD_TIMEOUT = 900


@dataclass(frozen=True)
class FormatChoice:
    key: str
    label: str
    format_selector: str
    height: int | None = None
    audio_only: bool = False


@dataclass(frozen=True)
class VideoInfo:
    title: str
    duration: int | None
    thumbnail: str | None
    choices: tuple[FormatChoice, ...]


@dataclass(frozen=True)
class DownloadedMedia:
    path: Path
    title: str
    size: int
    choice: FormatChoice


def _check_platform(url: str, expected_platform: str = "YouTube") -> None:
    parsed = validate_and_detect_url(url)
    if parsed.platform != expected_platform:
        raise ValueError(f"این گزینه فقط برای لینک‌های {expected_platform} است.")


def _subprocess_env(home: str) -> dict:
    """Minimal environment: no inherited secrets, cookies, or proxy settings."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": home,
        "LANG": "C.UTF-8",
        "XDG_CONFIG_HOME": home,
    }


def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the whole process group started with start_new_session=True."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    _kill_process_tree(proc)
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        pass


def _choices(formats: list[dict]) -> tuple[FormatChoice, ...]:
    heights = sorted({
        int(f["height"]) for f in formats
        if f.get("height") and f.get("vcodec") != "none"
        and (f.get("acodec") != "none" or f.get("vcodec"))
    }, reverse=True)
    choices: list[FormatChoice] = []
    for height in (1080, 720, 480):
        if any(h >= height for h in heights):
            below = [h for h in heights if h <= height]
            # Only offer a capped option when something at-or-below the cap
            # actually exists; otherwise the label would promise a height the
            # selector cannot deliver (e.g. a 4K-only video).
            if not below:
                continue
            actual = max(below)
            # Selector caps the requested maximum and falls back to best below it.
            choices.append(FormatChoice(str(height), f"ویدیو تا {actual}p", f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best", actual))
    if heights:
        max_height = max(heights)
        if not choices:
            # All available heights are outside the preset caps (e.g. 1440p only).
            choices.append(FormatChoice("best", f"ویدیو {max_height}p", "bestvideo+bestaudio/best", max_height))
        else:
            choices.append(FormatChoice("best", f"بهترین کیفیت ({max_height}p)", "bestvideo+bestaudio/best", max_height))
    if any(f.get("acodec") not in (None, "none") for f in formats):
        choices.append(FormatChoice("mp3", "فقط صدا — MP3", "bestaudio/best", None, True))
    return tuple(choices)


def _parse_info(stdout: bytes) -> dict:
    """Parse yt-dlp JSON; yt-dlp can exit 0 while printing a bare `null`."""
    try:
        info = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("پاسخ دانلودگر قابل خواندن نبود.") from exc
    if not isinstance(info, dict):
        raise RuntimeError("پاسخ دانلودگر قابل خواندن نبود.")
    return info


async def inspect(url: str, expected_platform: str = "YouTube") -> VideoInfo:
    parsed = await validate_public_destination(url)
    if parsed.platform != expected_platform:
        raise ValueError(f"لینک متعلق به {expected_platform} نیست.")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-warnings", "--no-playlist",
        "--dump-single-json", "--skip-download", "--socket-timeout", "20", "--retries", "1", url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=_subprocess_env("/tmp"), start_new_session=True,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_INSPECT_TIMEOUT)
    except asyncio.TimeoutError:
        await _terminate(proc)
        raise TimeoutError("اطلاعات رسانه در زمان مجاز دریافت نشد.") from None
    except asyncio.CancelledError:
        await _terminate(proc)
        raise
    if proc.returncode != 0:
        raise RuntimeError("استخراج اطلاعات ناموفق بود.")
    if len(stdout) > 5 * 1024 * 1024:
        raise ValueError("پاسخ اطلاعات رسانه بیش از حد بزرگ است.")
    info = _parse_info(stdout)
    title = str(info.get("title") or "media")[:200]
    duration = info.get("duration")
    if not isinstance(duration, int):
        duration = None
    return VideoInfo(title, duration, info.get("thumbnail"), _choices(info.get("formats") or []))


async def download(url: str, choice: FormatChoice, directory: str,
                   progress: Callable[[dict], None] | None = None,
                   expected_platform: str = "YouTube",
                   max_bytes: int = DEFAULT_MAX_BYTES) -> DownloadedMedia:
    parsed = await validate_public_destination(url)
    if parsed.platform != expected_platform:
        raise ValueError(f"لینک متعلق به {expected_platform} نیست.")
    if max_bytes < 1024 * 1024:
        raise ValueError("سقف حجم نامعتبر است.")
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    output_template = str(root / "%(title).80s-%(id)s.%(ext)s")
    args = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-warnings", "--no-playlist",
            "--newline", "--restrict-filenames", "--no-continue", "--overwrites",
            "--max-filesize", str(max_bytes),
            "--socket-timeout", "20", "--retries", "1", "--format", choice.format_selector,
            "--output", output_template]
    if choice.audio_only:
        args.extend(["--extract-audio", "--audio-format", "mp3", "--audio-quality", "192K"])
    args.append(url)
    env = _subprocess_env(str(root))
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT,
                                                env=env, start_new_session=True)

    async def consume() -> None:
        assert proc.stdout is not None
        while line := await proc.stdout.readline():
            if not progress:
                continue
            text = line.decode("utf-8", "replace")
            match = re.search(r"([0-9.]+)%.*?at\s+([0-9.]+\w+/s).*?ETA\s+([0-9:]+)", text)
            if match:
                try:
                    pct = min(100.0, float(match.group(1)))
                    speed_text = match.group(2).upper().replace("/S", "")
                    number = float(re.match(r"[0-9.]+", speed_text).group())
                    multiplier = 1024 ** (2 if "G" in speed_text else 1 if "M" in speed_text else 0)
                    eta_parts = [int(part) for part in match.group(3).split(":")]
                    eta = sum(value * (60 ** i) for i, value in enumerate(reversed(eta_parts)))
                    # percent is sent directly; downloaded/total remain for
                    # generic progress consumers (percent is authoritative).
                    progress({"status": "downloading", "percent": pct,
                              "downloaded_bytes": int(pct * 100), "total_bytes_estimate": 10000,
                              "speed": number * multiplier, "eta": eta})
                except (ValueError, AttributeError):
                    pass

    reader = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(proc.wait(), timeout=_DOWNLOAD_TIMEOUT)
        await reader
    except asyncio.TimeoutError:
        await _terminate(proc)
        reader.cancel()
        raise TimeoutError("دانلود در زمان مجاز کامل نشد.") from None
    except asyncio.CancelledError:
        await _terminate(proc)
        reader.cancel()
        raise
    if proc.returncode != 0:
        raise RuntimeError("دانلودگر با خطا متوقف شد.")
    candidates = [p.resolve() for p in root.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    if not candidates:
        raise RuntimeError("فایل خروجی ساخته نشد.")
    output = max(candidates, key=lambda p: p.stat().st_size)
    ensure_child_path(str(root), str(output))
    size = output.stat().st_size
    if size <= 0 or size > max_bytes:
        output.unlink(missing_ok=True)
        raise ValueError("حجم فایل خارج از محدودهٔ مجاز است.")
    return DownloadedMedia(output, output.stem[:200], size, choice)
