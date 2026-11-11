import asyncio
from pathlib import Path

import pytest

from services import instagram
from services.youtube import VideoInfo, FormatChoice


def test_instagram_inspection_filters_audio_choice(monkeypatch):
    choices = (
        FormatChoice("720", "ویدیو تا 720p", "best", 720),
        FormatChoice("mp3", "MP3", "audio", None, True),
    )
    async def fake_inspect(url, expected_platform):
        assert expected_platform == "Instagram"
        return VideoInfo("public reel", 30, None, choices)
    monkeypatch.setattr(instagram, "_inspect", fake_inspect)
    result = asyncio.run(instagram.inspect("https://www.instagram.com/reel/example/"))
    assert result.title == "public reel"
    assert len(result.choices) == 1
    assert not result.choices[0].audio_only


def test_instagram_rejects_audio(monkeypatch, tmp_path):
    audio = FormatChoice("mp3", "MP3", "bestaudio", None, True)
    with pytest.raises(ValueError):
        asyncio.run(instagram.download("https://www.instagram.com/reel/example/", audio, str(tmp_path)))


def test_shortcode_to_media_id_known_values():
    # Base64 alphabet A=0, B=1, ..., a=26, 0=52, -=62, _=63 (big-endian).
    assert instagram.shortcode_to_media_id("B") == 1
    assert instagram.shortcode_to_media_id("_") == 63
    assert instagram.shortcode_to_media_id("BA") == 64  # 1*64 + 0
    assert instagram.shortcode_to_media_id("cAR0plZ-") == 123164439238270
    assert instagram.shortcode_to_media_id("") is None
    assert instagram.shortcode_to_media_id("!!!!!") is None
    assert instagram.shortcode_to_media_id("a" * 17) is None


def test_shortcode_to_media_id_roundtrip():
    alphabet = instagram._B64_ALPHABET
    def to_shortcode(media_id: int) -> str:
        chars = []
        while media_id:
            chars.append(alphabet[media_id % 64])
            media_id //= 64
        return "".join(reversed(chars))
    for media_id in (1, 63, 64, 4095, 2**50 + 12345, 2943849384756384):
        code = to_shortcode(media_id)
        assert instagram.shortcode_to_media_id(code) == media_id


@pytest.mark.parametrize(("url", "expected"), [
    ("https://www.instagram.com/p/CxYzAbCdEf1/", "ok"),
    ("https://www.instagram.com/reel/CxYzAbCdEf1/", "ok"),
    ("https://www.instagram.com/tv/CxYzAbCdEf1/", "ok"),
    ("https://www.instagram.com/stories/user/123/", None),
    ("https://www.instagram.com/explore/", None),
])
def test_extract_shortcode_paths(url, expected):
    shortcode = instagram._extract_shortcode(url)
    if expected is None:
        assert shortcode is None
    else:
        assert shortcode is not None


def test_best_image_candidate_only_official_cdn_https(monkeypatch):
    entry = {"image_versions2": {"candidates": [
        {"url": "https://evil.example/fake.jpg", "width": 2000, "mime_type": "image/jpeg"},
        {"url": "http://scontent.cdninstagram.com/insecure.jpg", "width": 1500, "mime_type": "image/jpeg"},
        {"url": "https://scontent.fbcdn.net/photo.jpg", "width": 1080, "mime_type": "image/jpeg"},
        {"url": "https://scontent.cdninstagram.com/photo.svg", "width": 900, "mime_type": "image/svg+xml"},
    ]}}
    picked = instagram._best_image_candidate(entry)
    assert picked is not None
    url, ext = picked
    assert "fbcdn.net" in url and ext == ".jpg"


def test_best_image_candidate_none_when_no_valid_candidate():
    assert instagram._best_image_candidate({"image_versions2": {"candidates": []}}) is None
    assert instagram._best_image_candidate({}) is None


def _patch_no_network(monkeypatch):
    async def fake_validate(url):
        from core.security import ValidatedURL
        return ValidatedURL(url, "Instagram")
    async def fake_ensure(host):
        return None
    monkeypatch.setattr(instagram, "validate_public_destination", fake_validate)
    monkeypatch.setattr(instagram, "ensure_public_host", fake_ensure)


def test_fetch_post_parses_carousel(monkeypatch):
    _patch_no_network(monkeypatch)
    payload = {"items": [{
        "media_type": 8,
        "caption": {"text": "عنوان کاروسل & تست"},
        "carousel_media": [{"media_type": 1} for _ in range(12)],
    }]}
    async def fake_api(session, url):
        assert "api/v1/media/" in url
        return payload
    monkeypatch.setattr(instagram, "_api_get_json", fake_api)
    post = asyncio.run(instagram.fetch_post("https://www.instagram.com/p/CxYzAbCdEf1/"))
    assert post.kind == "carousel"
    assert post.count == 10  # capped at Telegram's media-group limit
    assert "&" in post.title


def test_fetch_post_reports_video_kind(monkeypatch):
    _patch_no_network(monkeypatch)
    payload = {"items": [{"media_type": 2, "caption": {"text": "reel"}}]}
    async def fake_api(session, url):
        return payload
    monkeypatch.setattr(instagram, "_api_get_json", fake_api)
    post = asyncio.run(instagram.fetch_post("https://www.instagram.com/p/CxYzAbCdEf1/"))
    assert post.kind == "video"


def test_fetch_images_writes_files_and_skips_foreign_cdn(monkeypatch, tmp_path):
    _patch_no_network(monkeypatch)
    entries = [
        {"image_versions2": {"candidates": [
            {"url": "https://scontent.cdninstagram.com/a.jpg", "width": 800, "mime_type": "image/jpeg"},
        ]}},
        {"image_versions2": {"candidates": [
            {"url": "https://evil.example/a.jpg", "width": 800, "mime_type": "image/jpeg"},
        ]}},
        {"image_versions2": {"candidates": [
            {"url": "https://scontent.fbcdn.net/b.webp", "width": 640, "mime_type": "image/webp"},
        ]}},
    ]
    async def fake_entries(url):
        return entries, "کپشن"
    counter = {"n": 0}
    async def fake_download(session, url):
        counter["n"] += 1
        return b"imagedata", "image/jpeg" if url.endswith(".jpg") else "image/webp"
    monkeypatch.setattr(instagram, "_post_entries", fake_entries)
    monkeypatch.setattr(instagram, "_download_image", fake_download)
    result = asyncio.run(instagram.fetch_images("https://www.instagram.com/p/CxYzAbCdEf1/", str(tmp_path)))
    assert len(result.paths) == 2  # evil.example skipped
    assert result.total_size == 18  # 2 × 9 bytes
    assert result.title == "کپشن"
    for path in result.paths:
        assert path.exists() and path.read_bytes() == b"imagedata"


def test_fetch_images_enforces_total_cap(monkeypatch, tmp_path):
    _patch_no_network(monkeypatch)
    monkeypatch.setattr(instagram, "_PER_IMAGE_MAX_BYTES", 50)
    entries = [
        {"image_versions2": {"candidates": [
            {"url": "https://scontent.cdninstagram.com/a.jpg", "width": 800, "mime_type": "image/jpeg"},
        ]}},
        {"image_versions2": {"candidates": [
            {"url": "https://scontent.cdninstagram.com/b.jpg", "width": 800, "mime_type": "image/jpeg"},
        ]}},
    ]
    async def fake_entries(url):
        return entries, "کپشن"
    async def fake_download(session, url):
        return b"x" * 40, "image/jpeg"
    monkeypatch.setattr(instagram, "_post_entries", fake_entries)
    monkeypatch.setattr(instagram, "_download_image", fake_download)
    with pytest.raises(ValueError, match="بیش از حد"):
        asyncio.run(instagram.fetch_images("https://www.instagram.com/p/CxYzAbCdEf1/",
                                           str(tmp_path), max_total_bytes=60))
    # No partial files remain after abort.
    assert list(tmp_path.iterdir()) == []
