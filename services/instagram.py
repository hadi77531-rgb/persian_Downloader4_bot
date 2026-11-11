"""Best-effort anonymous Instagram adapter.

- Video posts/reels: yt-dlp (unchanged).
- Image posts and photo carousels: Instagram's public web API
  (``/api/v1/media/{id}/info/`` with the well-known public ``x-ig-app-id``).
  No credentials, cookies, or login are used anywhere; everything is
  anonymous and fails gracefully when Instagram blocks anonymous access
  (typical on datacenter IPs).

Image CDN fetches are restricted to official CDN domains, HTTPS-only,
DNS preflighted for public addresses, hard-capped per image and in total.
"""
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from core.security import ensure_public_host, host_matches_domain, validate_public_destination
from services.youtube import (
    DEFAULT_MAX_BYTES,
    DownloadedMedia,
    FormatChoice,
    VideoInfo,
    download as _download,
    inspect as _inspect,
)

_IG_APP_ID = "936619743392459"  # well-known public Instagram web app id (not a secret)
_IG_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_API_URL_TEMPLATE = "https://www.instagram.com/api/v1/media/{media_id}/info/"
_API_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=10)
_API_MAX_BYTES = 5 * 1024 * 1024
_IMAGE_TIMEOUT = aiohttp.ClientTimeout(total=45, connect=10)
_PER_IMAGE_MAX_BYTES = 10 * 1024 * 1024
_MAX_IMAGES = 10  # Telegram media groups accept at most 10 items
_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
# Media URLs come from the API response; only official Instagram CDN domains are accepted.
_CDN_BASE_DOMAINS = ("cdninstagram.com", "fbcdn.net", "instagram.com")
_B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

_MEDIA_TYPE_IMAGE = 1
_MEDIA_TYPE_VIDEO = 2
_MEDIA_TYPE_CAROUSEL = 8


@dataclass(frozen=True)
class ImagePost:
    kind: str  # "image" | "carousel"
    title: str
    count: int  # number of deliverable images (capped at _MAX_IMAGES)


@dataclass(frozen=True)
class DownloadedImageSet:
    paths: tuple[Path, ...]
    title: str
    total_size: int


def shortcode_to_media_id(code: str) -> int | None:
    """Instagram shortcodes are the media id in base64 (A=0, a=26, 0=52, -=62, _=63)."""
    code = code.strip()
    if not code or len(code) > 16 or any(ch not in _B64_ALPHABET for ch in code):
        return None
    media_id = 0
    for ch in code:
        media_id = media_id * 64 + _B64_ALPHABET.index(ch)
    return media_id if media_id > 0 else None


def _extract_shortcode(url: str) -> str | None:
    parts = urlsplit(url)
    segments = [seg for seg in parts.path.split("/") if seg]
    if len(segments) >= 2 and segments[0] in ("p", "reel", "tv"):
        return shortcode_to_media_id(segments[1])
    return None


async def _api_get_json(session: aiohttp.ClientSession, url: str) -> dict:
    """GET with no redirects: a redirect (e.g. to /accounts/login) means blocked."""
    async with session.get(url, headers={
        "x-ig-app-id": _IG_APP_ID,
        "User-Agent": _IG_UA,
        "Accept": "application/json",
    }, allow_redirects=False) as resp:
        if resp.status != 200:
            raise RuntimeError("دسترسی ناشناس به اینستاگرام مسدود شد.")
        body = await resp.read()
    if len(body) > _API_MAX_BYTES:
        raise ValueError("پاسخ اینستاگرام بیش از حد بزرگ است.")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("پاسخ اینستاگرام قابل خواندن نبود.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("پاسخ اینستاگرام قابل خواندن نبود.")
    return payload


def _caption_of(item: dict) -> str:
    caption = item.get("caption")
    if isinstance(caption, dict):
        return str(caption.get("text") or "").strip()
    return ""


def _post_kind(item: dict) -> str:
    media_type = item.get("media_type")
    if media_type == _MEDIA_TYPE_CAROUSEL and item.get("carousel_media"):
        return "carousel"
    if media_type == _MEDIA_TYPE_IMAGE:
        return "image"
    return "video"


async def fetch_post(url: str) -> ImagePost:
    """Metadata-only lookup used to decide the image/carousel flow."""
    validated = await validate_public_destination(url)
    shortcode = _extract_shortcode(validated.url)
    if shortcode is None:
        raise ValueError("این نوع لینک Instagram پشتیبانی نمی‌شود؛ لینک پست یا ریل بفرستید.")
    await ensure_public_host("www.instagram.com")
    async with aiohttp.ClientSession(timeout=_API_TIMEOUT) as session:
        payload = await _api_get_json(session, _API_URL_TEMPLATE.format(media_id=shortcode))
    items = payload.get("items")
    item = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None
    if item is None:
        raise RuntimeError("پست در پاسخ اینستاگرام پیدا نشد.")
    kind = _post_kind(item)
    if kind == "carousel":
        count = min(len(item.get("carousel_media") or []), _MAX_IMAGES)
        return ImagePost(kind, _caption_of(item)[:100] or "Instagram", max(count, 1))
    if kind == "image":
        return ImagePost("image", _caption_of(item)[:100] or "Instagram", 1)
    return ImagePost("video", _caption_of(item)[:100] or "Instagram", 0)


def _best_image_candidate(entry: dict) -> tuple[str, str] | None:
    """Pick the highest-resolution JPEG/PNG/WebP candidate: returns (url, ext)."""
    versions = entry.get("image_versions2") if isinstance(entry, dict) else None
    candidates = versions.get("candidates") if isinstance(versions, dict) else None
    if not isinstance(candidates, list):
        return None
    best: tuple[int, str, str] | None = None  # (width, url, ext)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        url = str(candidate.get("url") or "")
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            continue
        if not any(host_matches_domain(parts.hostname, base) for base in _CDN_BASE_DOMAINS):
            continue
        content_type = str(candidate.get("mime_type") or "image/jpeg").split(";")[0].strip().lower()
        ext = _IMAGE_TYPES.get(content_type)
        if ext is None:
            continue
        width = int(candidate.get("width") or 0)
        if best is None or width > best[0]:
            best = (width, url, ext)
    return (best[1], best[2]) if best else None


async def _download_image(session: aiohttp.ClientSession, url: str) -> tuple[bytes, str]:
    async with session.get(url, headers={"User-Agent": _IG_UA}, allow_redirects=False) as resp:
        if resp.status != 200:
            raise RuntimeError("دریافت تصویر از CDN ناموفق بود.")
        content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type not in _IMAGE_TYPES:
            raise RuntimeError("قالب فایل تصویر پشتیبانی نمی‌شود.")
        declared = int(resp.headers.get("Content-Length") or 0)
        if declared > _PER_IMAGE_MAX_BYTES:
            raise ValueError("حجم تصویر بیش از حد مجاز است.")
        buffer = bytearray()
        async for chunk in resp.content.iter_chunked(65536):
            buffer.extend(chunk)
            if len(buffer) > _PER_IMAGE_MAX_BYTES:
                raise ValueError("حجم تصویر بیش از حد مجاز است.")
        return bytes(buffer), content_type


async def _post_entries(url: str) -> tuple[list[dict], str]:
    """Fetch media metadata and return the image entries plus a caption."""
    validated = await validate_public_destination(url)
    shortcode = _extract_shortcode(validated.url)
    if shortcode is None:
        raise ValueError("این نوع لینک Instagram پشتیبانی نمی‌شود؛ لینک پست یا ریل بفرستید.")
    await ensure_public_host("www.instagram.com")
    async with aiohttp.ClientSession(timeout=_API_TIMEOUT) as session:
        payload = await _api_get_json(session, _API_URL_TEMPLATE.format(media_id=shortcode))
    items = payload.get("items")
    item = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None
    if item is None:
        raise RuntimeError("پست در پاسخ اینستاگرام پیدا نشد.")
    kind = _post_kind(item)
    if kind == "carousel":
        entries = [e for e in (item.get("carousel_media") or []) if isinstance(e, dict)][:_MAX_IMAGES]
    elif kind == "image":
        entries = [item]
    else:
        raise ValueError("این پست ویدیوست و از مسیر ویدیو باید پردازش شود.")
    return entries, _caption_of(item)[:100]


async def fetch_images(url: str, directory: str,
                       max_total_bytes: int = 100 * 1024 * 1024) -> DownloadedImageSet:
    """Download an image post or photo carousel (max 10 images) into ``directory``."""
    if max_total_bytes < _PER_IMAGE_MAX_BYTES:
        raise ValueError("سقف حجم نامعتبر است.")
    entries, caption = await _post_entries(url)
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    total = 0
    async with aiohttp.ClientSession(timeout=_IMAGE_TIMEOUT) as session:
        for index, entry in enumerate(entries):
            picked = _best_image_candidate(entry)
            if picked is None:
                continue
            image_url, ext = picked
            host = urlsplit(image_url).hostname or ""
            await ensure_public_host(host)
            data, content_type = await _download_image(session, image_url)
            total += len(data)
            if total > max_total_bytes:
                for written in paths:
                    written.unlink(missing_ok=True)
                raise ValueError("حجم مجموع تصاویر بیش از حد مجاز است.")
            path = root / f"img_{index:02d}{ext}"
            path.write_bytes(data)
            paths.append(path)
    if not paths:
        raise RuntimeError("تصویری قابل دریافت پیدا نشد.")
    return DownloadedImageSet(tuple(paths), caption or "Instagram", total)


async def inspect(url: str) -> VideoInfo:
    info = await _inspect(url, expected_platform="Instagram")
    choices = tuple(choice for choice in info.choices if not choice.audio_only)
    return VideoInfo(info.title, info.duration, info.thumbnail, choices)


async def download(url: str, choice: FormatChoice, directory: str, progress=None,
                   max_bytes: int = DEFAULT_MAX_BYTES) -> DownloadedMedia:
    if choice.audio_only:
        # MP3 is not an advertised Instagram feature in this phase.
        raise ValueError("Instagram برای این نسخه فقط ویدیو است.")
    return await _download(url, choice, directory, progress, expected_platform="Instagram", max_bytes=max_bytes)
