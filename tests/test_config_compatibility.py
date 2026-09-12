import pytest
from app.config import get_settings


@pytest.fixture
def config_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("ADMIN_IDS", "ADMIN_USER_IDS", "MEDIA_PATH", "MEDIA_DIR"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        "BOT_TOKEN": "123456:test-token", "PUBLIC_BASE_URL": "https://example.com",
        "YOOKASSA_SHOP_ID": "test", "YOOKASSA_SECRET_KEY": "test",
        "ENABLE_LEGACY_YOOMONEY": "false",
    }.items():
        monkeypatch.setenv(key, value)
    return tmp_path, monkeypatch


def test_legacy_env_names_remain_supported(config_env):
    path, env = config_env
    env.setenv("ADMIN_USER_IDS", "123, 456")
    env.setenv("MEDIA_DIR", str(path / "old-photos"))
    settings = get_settings()
    assert settings.admin_ids == frozenset({123, 456})
    assert settings.media_path == str(path / "old-photos")


def test_explicit_new_settings_take_precedence(config_env):
    path, env = config_env
    env.setenv("ADMIN_USER_IDS", "123")
    env.setenv("ADMIN_IDS", "")
    env.setenv("MEDIA_DIR", str(path / "old-photos"))
    env.setenv("MEDIA_PATH", str(path / "new-photos"))
    settings = get_settings()
    assert not settings.admin_ids
    assert settings.media_path == str(path / "new-photos")


def test_existing_default_media_directory_is_kept(config_env):
    path, env = config_env
    (path / "data/media").mkdir(parents=True)
    assert get_settings().media_path == "data/media"
    env.setenv("MEDIA_DIR", "")
    assert get_settings().media_path == "data/media"
    (path / "data/material_images").mkdir()
    assert get_settings().media_path == "data/material_images"


def test_invalid_legacy_admin_ids_are_rejected(config_env):
    _, env = config_env
    env.setenv("ADMIN_USER_IDS", "-1")
    with pytest.raises(RuntimeError, match="ADMIN_IDS"):
        get_settings()
