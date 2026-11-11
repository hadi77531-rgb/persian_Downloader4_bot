import asyncio
import socket
from pathlib import Path

import pytest

from core.security import (
    ensure_child_path,
    ensure_public_host,
    host_matches_domain,
    validate_public_destination,
)


def _record(address):
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))


def test_dns_rejects_private_address(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [_record("127.0.0.1")])
    with pytest.raises(ValueError, match="عمومی نیست"):
        asyncio.run(validate_public_destination("https://www.youtube.com/watch?v=x"))


def test_dns_accepts_public_address(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [_record("142.250.72.14")])
    result = asyncio.run(validate_public_destination("https://www.youtube.com/watch?v=x"))
    assert result.platform == "YouTube"


def test_output_path_must_remain_under_job_directory(tmp_path):
    root = tmp_path / "job"
    root.mkdir()
    good = root / "media.mp4"
    assert ensure_child_path(str(root), str(good)) == str(good.resolve())
    with pytest.raises(ValueError):
        ensure_child_path(str(root), str(root / ".." / "escape.mp4"))


def test_host_matches_domain_is_anchored():
    assert host_matches_domain("youtube.com", "youtube.com")
    assert host_matches_domain("m.youtube.com", "youtube.com")
    assert host_matches_domain("scontent.cdninstagram.com", "cdninstagram.com")
    assert not host_matches_domain("youtube.com.evil.example", "youtube.com")
    assert not host_matches_domain("evil-youtube.com", "youtube.com")
    assert not host_matches_domain("", "youtube.com")


def test_ensure_public_host_rejects_private_resolution(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [_record("10.0.0.5")])
    with pytest.raises(ValueError, match="عمومی نیست"):
        asyncio.run(ensure_public_host("cdn.example.com"))


def test_ensure_public_host_rejects_empty_host():
    with pytest.raises(ValueError):
        asyncio.run(ensure_public_host(""))


def test_ensure_public_host_accepts_public_resolution(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [_record("31.13.72.36")])
    asyncio.run(ensure_public_host("scontent.cdninstagram.com"))  # must not raise
