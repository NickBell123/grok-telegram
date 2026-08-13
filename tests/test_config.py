import os
import pytest
from grok_telegram.config import Config, ConfigError


def test_loads_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("ALLOWED_USER_ID", "42")
    monkeypatch.setenv("DEFAULT_CHAT_ID", "42")
    monkeypatch.setenv("PUSH_TOKEN", "secret")
    cfg = Config.from_env()
    assert cfg.telegram_bot_token == "tok"
    assert cfg.allowed_user_id == 42
    assert cfg.default_chat_id == "42"
    assert cfg.push_token == "secret"
    assert cfg.push_host == "127.0.0.1"
    assert cfg.push_port == 8788
    assert cfg.grok_model == "grok-4.6"


def test_missing_required_raises(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ALLOWED_USER_ID", "42")
    monkeypatch.setenv("DEFAULT_CHAT_ID", "42")
    monkeypatch.setenv("PUSH_TOKEN", "secret")
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        Config.from_env()


def test_allowed_user_id_must_be_int(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("ALLOWED_USER_ID", "not-a-number")
    monkeypatch.setenv("DEFAULT_CHAT_ID", "42")
    monkeypatch.setenv("PUSH_TOKEN", "secret")
    with pytest.raises(ConfigError, match="ALLOWED_USER_ID"):
        Config.from_env()


def test_grok_model_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("ALLOWED_USER_ID", "42")
    monkeypatch.setenv("DEFAULT_CHAT_ID", "42")
    monkeypatch.setenv("PUSH_TOKEN", "secret")
    monkeypatch.setenv("GROK_MODEL", "grok-4.5")
    cfg = Config.from_env()
    assert cfg.grok_model == "grok-4.5"
