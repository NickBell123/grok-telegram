import pytest
from grok_telegram.bot import HELP_TEXT, is_authorized, parse_command


def test_authorized_user():
    assert is_authorized(user_id=42, allowed_user_id=42) is True


def test_unauthorized_user():
    assert is_authorized(user_id=99, allowed_user_id=42) is False


def test_unauthorized_when_none():
    assert is_authorized(user_id=None, allowed_user_id=42) is False


def test_parse_known_command():
    assert parse_command("/reset") == ("reset", "")
    assert parse_command("/cd /tmp") == ("cd", "/tmp")
    assert parse_command("/cd  /tmp/foo bar") == ("cd", "/tmp/foo bar")
    assert parse_command("/photo /tmp/a.png") == ("photo", "/tmp/a.png")
    assert parse_command("/file report.pdf") == ("file", "report.pdf")
    assert parse_command("/tts auto") == ("tts", "auto")
    assert parse_command("/say hello") == ("say", "hello")
    assert parse_command("/speech local") == ("speech", "local")
    assert parse_command("/provider grok") == ("provider", "grok")
    assert parse_command("/model grok-4.6") == ("model", "grok-4.6")
    assert parse_command("/help") == ("help", "")
    assert parse_command("/help@Grok121_bot") == ("help", "")


def test_parse_non_command():
    assert parse_command("hello world") == (None, "hello world")


def test_parse_empty():
    assert parse_command("") == (None, "")
