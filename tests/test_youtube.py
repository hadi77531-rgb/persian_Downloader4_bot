import pytest

from services.youtube import _choices, _parse_info


def test_quality_options_reflect_available_heights():
    choices = _choices([
        {"height": 720, "vcodec": "avc1", "acodec": "none"},
        {"height": 360, "vcodec": "avc1", "acodec": "mp4a"},
        {"height": None, "vcodec": "none", "acodec": "mp4a"},
    ])
    labels = [c.label for c in choices]
    assert any("720p" in label for label in labels)
    assert not any("1080p" in label for label in labels)
    assert "فقط صدا — MP3" in labels


def test_audio_only_when_audio_format_exists():
    choices = _choices([{"height": 360, "vcodec": "avc1", "acodec": "none"}])
    assert not any(c.audio_only for c in choices)


def test_4k_only_video_gets_a_single_honest_best_option():
    choices = _choices([{"height": 2160, "vcodec": "avc1", "acodec": "none"}])
    assert len(choices) == 1
    assert choices[0].key == "best"
    assert "2160p" in choices[0].label
    # No misleading capped buttons promising heights the selector cannot deliver.
    assert "1080" not in choices[0].label and "480" not in choices[0].label


def test_heights_above_caps_produce_no_misleading_capped_buttons():
    choices = _choices([
        {"height": 2160, "vcodec": "avc1", "acodec": "none"},
        {"height": 1440, "vcodec": "avc1", "acodec": "none"},
    ])
    assert [c.key for c in choices] == ["best"]
    assert "ویدیو 2160p" == choices[0].label


def test_normal_video_gets_capped_options_and_best():
    choices = _choices([
        {"height": 1080, "vcodec": "avc1", "acodec": "none"},
        {"height": 720, "vcodec": "avc1", "acodec": "none"},
        {"height": 480, "vcodec": "avc1", "acodec": "none"},
        {"height": 360, "vcodec": "avc1", "acodec": "mp4a"},
    ])
    keys = [c.key for c in choices]
    assert keys == ["1080", "720", "480", "best", "mp3"]
    assert choices[0].label == "ویدیو تا 1080p"
    assert choices[1].label == "ویدیو تا 720p"
    assert choices[2].label == "ویدیو تا 480p"
    assert choices[3].label == "بهترین کیفیت (1080p)"


def test_choice_keys_are_unique():
    for formats in (
        [{"height": 2160, "vcodec": "avc1", "acodec": "none"}],
        [{"height": 100, "vcodec": "avc1", "acodec": "none"}],
        [{"height": 1080, "vcodec": "avc1", "acodec": "mp4a"}],
    ):
        keys = [c.key for c in _choices(formats)]
        assert len(keys) == len(set(keys)), f"duplicate choice keys for {formats}"


def test_parse_info_accepts_object_json():
    assert _parse_info(b'{"title": "x"}') == {"title": "x"}


@pytest.mark.parametrize("payload", [b"null", b"[]", b'"text"', b"not json", b""])
def test_parse_info_rejects_non_object_payloads(payload):
    with pytest.raises(RuntimeError):
        _parse_info(payload)
