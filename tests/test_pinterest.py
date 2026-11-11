import asyncio
import pytest

from services import pinterest
from services.youtube import VideoInfo, FormatChoice


def test_pinterest_inspection_filters_audio(monkeypatch):
    choices = (
        FormatChoice("480", "video 480p", "best", 480),
        FormatChoice("mp3", "MP3", "audio", None, True),
    )
    async def fake_inspect(url, expected_platform):
        assert expected_platform == "Pinterest"
        return VideoInfo("video pin", 12, None, choices)
    monkeypatch.setattr(pinterest, "_inspect", fake_inspect)
    result = asyncio.run(pinterest.inspect("https://www.pinterest.com/pin/123/"))
    assert result.title == "video pin"
    assert len(result.choices) == 1
    assert not result.choices[0].audio_only


def test_pinterest_refuses_audio(monkeypatch, tmp_path):
    audio = FormatChoice("mp3", "MP3", "audio", None, True)
    with pytest.raises(ValueError):
        asyncio.run(pinterest.download("https://www.pinterest.com/pin/123/", audio, str(tmp_path)))
