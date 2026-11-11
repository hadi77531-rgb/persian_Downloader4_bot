import pytest

from core.security import validate_and_detect_url


@pytest.mark.parametrize(("url", "platform"), [
    ("https://www.youtube.com/watch?v=abc", "YouTube"),
    ("https://youtu.be/abc", "YouTube"),
    ("https://m.youtube.com/watch?v=abc", "YouTube"),
    ("https://music.youtube.com/watch?v=abc", "YouTube"),
    ("https://www.instagram.com/reel/example/", "Instagram"),
    ("https://pin.it/example", "Pinterest"),
    ("https://www.pinterest.com/pin/123/", "Pinterest"),
    ("https://fa.pinterest.com/pin/123/", "Pinterest"),
])
def test_detect_supported_domains(url, platform):
    assert validate_and_detect_url(url).platform == platform


@pytest.mark.parametrize("url", [
    "http://youtube.com/watch?v=x",
    "https://youtube.com.evil.example/watch?v=x",
    "https://evil-youtube.com/watch?v=x",
    "https://youtube.com.evil.com/watch?v=x",
    "https://youtube.com@evil.example/watch?v=x",
    "https://example.com/",
    "ftp://instagram.com/a",
    "https://youtube.com:444/watch?v=x",
    "https://youtube.com:bad/watch?v=x",
    "https://user:pass@youtube.com/watch?v=x",
    "",
    "https://youtube.com/a b",
    "https://142.250.72.14/watch?v=x",
])
def test_reject_unsafe_or_unsupported_urls(url):
    with pytest.raises(ValueError):
        validate_and_detect_url(url)
