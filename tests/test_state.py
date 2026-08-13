from pathlib import Path
from grok_telegram.state import StateStore, ChatState


def test_set_and_get_roundtrip(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    store.set("123", ChatState(session_id="abc", cwd="/work/proj"))
    again = StateStore(tmp_path / "state.json")
    assert again.get("123") == ChatState(session_id="abc", cwd="/work/proj")


def test_get_missing_returns_default_cwd(tmp_path: Path):
    store = StateStore(tmp_path / "state.json", default_cwd="/tmp")
    s = store.get("999")
    assert s.session_id is None
    assert s.cwd == "/tmp"


def test_clear_session_keeps_cwd(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    store.set("123", ChatState(session_id="abc", cwd="/x"))
    store.clear_session("123")
    s = store.get("123")
    assert s.session_id is None
    assert s.cwd == "/x"


def test_set_cwd(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    store.set_cwd("123", "/new")
    assert store.get("123").cwd == "/new"


def test_tts_mode_default_and_set(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    assert store.get("1").tts_mode == "auto"
    store.set_tts_mode("1", "on")
    assert store.get("1").tts_mode == "on"
    store.set_session("1", "sess")
    store.clear_session("1")
    assert store.get("1").tts_mode == "on"
    store.set_tts_mode("1", "off")
    assert store.get("1").tts_mode == "off"


def test_speech_provider_override_and_default(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    assert store.get("1").speech_provider is None
    store.set_speech_provider("1", "local")
    assert store.get("1").speech_provider == "local"
    store.set_tts_mode("1", "on")
    assert store.get("1").speech_provider == "local"
    store.set_speech_provider("1", None)
    assert store.get("1").speech_provider is None


def test_grok_model_override_and_default(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    assert store.get("1").grok_model is None
    store.set_grok_model("1", "grok-4.6")
    assert store.get("1").grok_model == "grok-4.6"
    store.set_session("1", "sess")
    store.clear_session("1")
    assert store.get("1").grok_model == "grok-4.6"
    store.set_grok_model("1", None)
    assert store.get("1").grok_model is None
