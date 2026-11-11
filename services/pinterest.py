"""Best-effort anonymous Pinterest video Pin adapter.

Pinterest extractor availability is unstable. yt-dlp is used only for video
Pins; still images are intentionally reported unsupported by this adapter.
"""
from services.youtube import (
    DEFAULT_MAX_BYTES,
    DownloadedMedia,
    FormatChoice,
    VideoInfo,
    download as _download,
    inspect as _inspect,
)


async def inspect(url: str) -> VideoInfo:
    info = await _inspect(url, expected_platform="Pinterest")
    video_choices = tuple(choice for choice in info.choices if not choice.audio_only)
    return VideoInfo(info.title, info.duration, info.thumbnail, video_choices)


async def download(url: str, choice: FormatChoice, directory: str, progress=None,
                   max_bytes: int = DEFAULT_MAX_BYTES) -> DownloadedMedia:
    if choice.audio_only:
        raise ValueError("Pinterest در این نسخه فقط برای ویدیو است.")
    return await _download(url, choice, directory, progress, expected_platform="Pinterest", max_bytes=max_bytes)
