import pytest

from core.config import Settings


def _settings(**kwargs) -> Settings:
    return Settings(bot_token="test-token", _env_file=None, **kwargs)


def test_admin_ids_parse_and_validate():
    assert _settings(admin_ids="1, 2,3").admin_id_set == frozenset({1, 2, 3})
    with pytest.raises(ValueError):
        _settings(admin_ids="one,two").admin_id_set


def test_max_file_mb_clamped_instead_of_crash():
    # Operators may set the documented 50 MB value; it must clamp, not fail startup.
    assert _settings(max_file_mb=50).max_file_mb == 49
    assert _settings(max_file_mb=500).max_file_mb == 49
    assert _settings(max_file_mb=25).max_file_mb == 25
    with pytest.raises(Exception):
        _settings(max_file_mb="zero")
