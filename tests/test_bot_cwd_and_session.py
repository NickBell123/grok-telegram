import os
from pathlib import Path

import pytest

from grok_telegram.bot import HELP_TEXT, Bot
from grok_telegram.config import Config
from grok_telegram.log import JsonlLogger
from grok_telegram.ratelimit import SlidingWindowLimiter
from grok_telegram.runner import RunnerEvent
from grok_telegram.state import StateStore

FAKE_TOKEN = "123456:AAHfakefakefakefakefakefakefakefake"

STALE_SESSION_STDERR = (
    'Session "bf0d22b8-4c2b-43cc-a002-4fed130425fe" not found locally, restoring from remote...\n'
    "Error: Failed to restore session from remote: fetching session record: session get failed: 404 Not Found\n"
)


class FakeBotApi:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.edits: list[tuple[int, str]] = []
        self.photos: list[tuple[str, str | None]] = []
        self.documents: list[tuple[str, str | None]] = []
        self._id = 0

    async def send_message(self, chat_id: str, text: str, parse_mode: str | None = None):
        self._id += 1
        self.sent.append((chat_id, text))
        return type("Msg", (), {"message_id": self._id})()

    async def edit_message_text(
        self, chat_id: str, message_id: int, text: str, parse_mode: str | None = None
    ) -> None:
        self.edits.append((message_id, text))

    async def send_chat_action(self, chat_id: str, action) -> None:
        self.actions = getattr(self, "actions", [])
        self.actions.append((chat_id, action))

    async def send_photo(self, chat_id: str, photo, caption: str | None = None):
        self._id += 1
        name = getattr(photo, "name", str(photo))
        self.photos.append((name, caption))
        return type("Msg", (), {"message_id": self._id})()

    async def send_document(self, chat_id: str, document, caption: str | None = None, filename: str | None = None):
        self._id += 1
        name = filename or getattr(document, "name", str(document))
        self.documents.append((name, caption))
        return type("Msg", (), {"message_id": self._id})()

    async def send_voice(self, chat_id: str, voice, caption: str | None = None):
        self._id += 1
        self.voices = getattr(self, "voices", [])
        self.voices.append(caption)
        return type("Msg", (), {"message_id": self._id})()


class FakeApp:
    def __init__(self):
        self.bot = FakeBotApi()


class ScriptedRunner:
    """Yields a fixed event list and records the session_id/cwd it was handed."""

    def __init__(self, events: list[RunnerEvent]):
        self.events = events
        self.seen_session: str | None = None
        self.seen_cwd: str | None = None
        self.called = False

    async def run(self, prompt: str, session_id, cwd: str):
        self.called = True
        self.seen_session = session_id
        self.seen_cwd = cwd
        for ev in self.events:
            yield ev

    def terminate(self) -> None:
        pass


def _build_bot(tmp_path: Path, runners: list) -> Bot:
    cfg = Config(
        telegram_bot_token=FAKE_TOKEN,
        allowed_user_id=42,
        default_chat_id="42",
        push_token="secret",
        state_dir=str(tmp_path),
    )
    handed_out = iter(runners)
    bot = Bot(
        config=cfg,
        state=StateStore(tmp_path / "state.json", default_cwd=str(tmp_path)),
        runner_factory=lambda: next(handed_out),
        logger=JsonlLogger(tmp_path / "log.jsonl"),
        limiter=SlidingWindowLimiter(max_events=100, window_seconds=3600),
    )
    bot.app = FakeApp()
    return bot


def _transcript(bot: Bot) -> str:
    """Everything the user actually saw, sent messages and edits alike."""
    return "\n".join([t for _, t in bot.app.bot.sent] + [t for _, t in bot.app.bot.edits])


# --- /cd validation -------------------------------------------------------


@pytest.mark.asyncio
async def test_cd_to_missing_directory_is_rejected_and_not_persisted(tmp_path: Path):
    bot = _build_bot(tmp_path, [])
    original = bot.state.get("chat-a").cwd

    await bot._handle_command("chat-a", "cd", str(tmp_path / "nope"))

    assert bot.state.get("chat-a").cwd == original
    assert "not a directory" in bot.app.bot.sent[-1][1]


@pytest.mark.asyncio
async def test_cd_to_a_file_is_rejected(tmp_path: Path):
    bot = _build_bot(tmp_path, [])
    f = tmp_path / "a-file"
    f.write_text("x")
    original = bot.state.get("chat-a").cwd

    await bot._handle_command("chat-a", "cd", str(f))

    assert bot.state.get("chat-a").cwd == original
    assert "not a directory" in bot.app.bot.sent[-1][1]


@pytest.mark.asyncio
async def test_cd_resolves_relative_path_against_current_cwd(tmp_path: Path):
    bot = _build_bot(tmp_path, [])
    start = tmp_path / "project"
    (start / "docs").mkdir(parents=True)
    bot.state.set_cwd("chat-a", str(start))

    await bot._handle_command("chat-a", "cd", "docs")

    assert bot.state.get("chat-a").cwd == str(start / "docs")


@pytest.mark.asyncio
async def test_cd_stores_an_absolute_normalised_path(tmp_path: Path):
    bot = _build_bot(tmp_path, [])
    (tmp_path / "project").mkdir()
    bot.state.set_cwd("chat-a", str(tmp_path / "project"))

    await bot._handle_command("chat-a", "cd", "..")

    stored = bot.state.get("chat-a").cwd
    assert os.path.isabs(stored)
    assert stored == str(tmp_path)


# --- stale session recovery ----------------------------------------------


@pytest.mark.asyncio
async def test_stale_session_is_cleared_and_the_turn_retried_fresh(tmp_path: Path):
    dead = ScriptedRunner([
        RunnerEvent(kind="error", data={"returncode": 1, "stderr": STALE_SESSION_STDERR}),
    ])
    fresh = ScriptedRunner([
        RunnerEvent(kind="session", data={"session_id": "new-session"}),
        RunnerEvent(kind="text", data={"text": "hello from the retry"}),
        RunnerEvent(kind="result", data={"cost_usd": 0.02, "session_id": "new-session"}),
    ])
    bot = _build_bot(tmp_path, [dead, fresh])
    bot.state.set_session("chat-a", "bf0d22b8-4c2b-43cc-a002-4fed130425fe")

    await bot._run_turn("chat-a", "hi")

    assert fresh.called, "a stale session should trigger one retry"
    assert fresh.seen_session is None, "the retry must run without --resume"
    assert bot.state.get("chat-a").session_id == "new-session"


@pytest.mark.asyncio
async def test_stale_session_retry_hides_the_error_and_shows_the_reply(tmp_path: Path):
    dead = ScriptedRunner([
        RunnerEvent(kind="error", data={"returncode": 1, "stderr": STALE_SESSION_STDERR}),
    ])
    fresh = ScriptedRunner([
        RunnerEvent(kind="text", data={"text": "hello from the retry"}),
        RunnerEvent(kind="result", data={"cost_usd": 0.02, "session_id": "new-session"}),
    ])
    bot = _build_bot(tmp_path, [dead, fresh])
    bot.state.set_session("chat-a", "bf0d22b8-4c2b-43cc-a002-4fed130425fe")

    await bot._run_turn("chat-a", "hi")

    seen = _transcript(bot)
    assert "Failed to restore session" not in seen
    assert "hello from the retry" in seen


@pytest.mark.asyncio
async def test_stale_session_is_not_retried_twice(tmp_path: Path):
    dead = ScriptedRunner([
        RunnerEvent(kind="error", data={"returncode": 1, "stderr": STALE_SESSION_STDERR}),
    ])
    also_dead = ScriptedRunner([
        RunnerEvent(kind="error", data={"returncode": 1, "stderr": STALE_SESSION_STDERR}),
    ])
    third = ScriptedRunner([RunnerEvent(kind="text", data={"text": "should not run"})])
    bot = _build_bot(tmp_path, [dead, also_dead, third])
    bot.state.set_session("chat-a", "bf0d22b8-4c2b-43cc-a002-4fed130425fe")

    await bot._run_turn("chat-a", "hi")

    assert third.called is False, "only one retry is allowed"
    assert "Failed to restore session" in _transcript(bot), "a second failure must surface"


@pytest.mark.asyncio
async def test_partial_output_before_a_stale_error_is_not_retried(tmp_path: Path):
    """If the user already saw a reply, silently re-running would duplicate work."""
    chatty = ScriptedRunner([
        RunnerEvent(kind="text", data={"text": "some real output"}),
        RunnerEvent(kind="error", data={"returncode": 1, "stderr": STALE_SESSION_STDERR}),
    ])
    second = ScriptedRunner([RunnerEvent(kind="text", data={"text": "should not run"})])
    bot = _build_bot(tmp_path, [chatty, second])
    bot.state.set_session("chat-a", "bf0d22b8-4c2b-43cc-a002-4fed130425fe")

    await bot._run_turn("chat-a", "hi")

    assert second.called is False


@pytest.mark.asyncio
async def test_unrelated_runner_error_is_not_retried(tmp_path: Path):
    boom = ScriptedRunner([
        RunnerEvent(kind="error", data={"returncode": 1, "stderr": "some other failure\n"}),
    ])
    second = ScriptedRunner([RunnerEvent(kind="text", data={"text": "should not run"})])
    bot = _build_bot(tmp_path, [boom, second])
    bot.state.set_session("chat-a", "sess-1")

    await bot._run_turn("chat-a", "hi")

    assert second.called is False
    assert "some other failure" in _transcript(bot)


# --- failed turns must not look successful --------------------------------


@pytest.mark.asyncio
async def test_failed_turn_is_not_marked_with_a_success_tick(tmp_path: Path):
    boom = ScriptedRunner([
        RunnerEvent(kind="error", data={"returncode": 1, "stderr": "some other failure\n"}),
    ])
    bot = _build_bot(tmp_path, [boom])

    await bot._run_turn("chat-a", "hi")

    seen = _transcript(bot)
    assert "✅" not in seen, "an errored turn must not render a success tick"
    assert "❌" in seen


@pytest.mark.asyncio
async def test_successful_turn_still_gets_a_tick_and_cost(tmp_path: Path):
    ok = ScriptedRunner([
        RunnerEvent(kind="text", data={"text": "all good"}),
        RunnerEvent(kind="result", data={"cost_usd": 0.0123, "session_id": "s1"}),
    ])
    bot = _build_bot(tmp_path, [ok])

    await bot._run_turn("chat-a", "hi")

    seen = _transcript(bot)
    assert "✅" in seen
    assert "$0.0123" in seen


@pytest.mark.asyncio
async def test_runner_error_renders_stderr_not_a_raw_dict(tmp_path: Path):
    boom = ScriptedRunner([
        RunnerEvent(kind="error", data={"returncode": 1, "stderr": "boom: it broke\n"}),
    ])
    bot = _build_bot(tmp_path, [boom])

    await bot._run_turn("chat-a", "hi")

    seen = _transcript(bot)
    assert "boom: it broke" in seen
    assert "'returncode'" not in seen, "should not dump the raw event dict"


# --- media: /photo /file and auto-attach ---------------------------------


@pytest.mark.asyncio
async def test_photo_command_sends_image(tmp_path: Path):
    img = tmp_path / "face.png"
    img.write_bytes(b"\x89PNG" + b"\0" * 40)
    bot = _build_bot(tmp_path, [])

    await bot._handle_command("chat-a", "photo", str(img))

    assert len(bot.app.bot.photos) == 1
    assert bot.app.bot.photos[0][1] == "face.png"


@pytest.mark.asyncio
async def test_file_command_sends_document(tmp_path: Path):
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.4 fake")
    bot = _build_bot(tmp_path, [])

    await bot._handle_command("chat-a", "file", str(doc))

    assert len(bot.app.bot.documents) == 1
    assert bot.app.bot.documents[0][0] == "report.pdf"


@pytest.mark.asyncio
async def test_photo_command_rejects_missing(tmp_path: Path):
    bot = _build_bot(tmp_path, [])
    await bot._handle_command("chat-a", "photo", "nope.png")
    assert bot.app.bot.photos == []
    assert "not a readable file" in bot.app.bot.sent[-1][1]


@pytest.mark.asyncio
async def test_turn_auto_sends_image_mentioned_in_text(tmp_path: Path):
    img = tmp_path / "out.png"
    img.write_bytes(b"pngbytes")
    ok = ScriptedRunner([
        RunnerEvent(kind="text", data={"text": f"Saved chart to {img}"}),
        RunnerEvent(kind="result", data={"cost_usd": 0.01, "session_id": "s1"}),
    ])
    bot = _build_bot(tmp_path, [ok])

    await bot._run_turn("chat-a", "make a chart")

    assert len(bot.app.bot.photos) == 1


# --- TTS mode -------------------------------------------------------------


class FakeSpeech:
    def __init__(self, ogg: Path):
        self.ogg = ogg
        self.spoken: list[str] = []
        self.transcribed: list[str] = []

    def available(self):
        return {"stt": True, "tts": True}

    async def synthesize(self, text: str, voice=None, max_chars=None):
        self.spoken.append(text)
        self.ogg.write_bytes(b"OggS")
        return self.ogg

    async def transcribe(self, path, language: str = "en"):
        self.transcribed.append(str(path))
        return "transcribed hello"


@pytest.mark.asyncio
async def test_tts_auto_speaks_only_on_voice_input(tmp_path: Path):
    ogg = tmp_path / "v.ogg"
    speech = FakeSpeech(ogg)
    ok = ScriptedRunner([
        RunnerEvent(kind="text", data={"text": "Hi from Grok"}),
        RunnerEvent(kind="result", data={"cost_usd": 0.01, "session_id": "s1"}),
    ])
    bot = _build_bot(tmp_path, [ok])
    bot.speech = speech
    bot.state.set_tts_mode("chat-a", "auto")

    await bot._run_turn("chat-a", "typed", voice_input=False)
    assert speech.spoken == []

    ok2 = ScriptedRunner([
        RunnerEvent(kind="text", data={"text": "Voice reply"}),
        RunnerEvent(kind="result", data={"cost_usd": 0.01, "session_id": "s1"}),
    ])
    bot2 = _build_bot(tmp_path, [ok2])
    bot2.speech = speech
    bot2.state.set_tts_mode("chat-a", "auto")
    await bot2._run_turn("chat-a", "from mic", voice_input=True)
    assert any("Voice reply" in s for s in speech.spoken)


@pytest.mark.asyncio
async def test_tts_on_speaks_text_turns(tmp_path: Path):
    speech = FakeSpeech(tmp_path / "v.ogg")
    ok = ScriptedRunner([
        RunnerEvent(kind="text", data={"text": "Always speak"}),
        RunnerEvent(kind="result", data={"cost_usd": 0.01, "session_id": "s1"}),
    ])
    bot = _build_bot(tmp_path, [ok])
    bot.speech = speech
    bot.state.set_tts_mode("chat-a", "on")
    await bot._run_turn("chat-a", "hi", voice_input=False)
    assert speech.spoken and "Always speak" in speech.spoken[0]


@pytest.mark.asyncio
async def test_tts_command_sets_mode(tmp_path: Path):
    bot = _build_bot(tmp_path, [])
    await bot._handle_command("chat-a", "tts", "off")
    assert bot.state.get("chat-a").tts_mode == "off"
    assert "off" in bot.app.bot.sent[-1][1]


@pytest.mark.asyncio
async def test_run_turn_sends_typing_action(tmp_path: Path):
    ok = ScriptedRunner([
        RunnerEvent(kind="text", data={"text": "hi"}),
        RunnerEvent(kind="result", data={"cost_usd": 0.01, "session_id": "s1"}),
    ])
    bot = _build_bot(tmp_path, [ok])
    await bot._run_turn("chat-a", "hello", voice_input=False)
    actions = getattr(bot.app.bot, "actions", [])
    assert actions, "expected at least one send_chat_action(typing)"
    assert actions[0][0] == "chat-a"


class DualFakeSpeech(FakeSpeech):
    def __init__(self, ogg: Path):
        super().__init__(ogg)
        self.provider = "grok"

    def available(self):
        # Both backends look ready in tests.
        return {"stt": True, "tts": True}


@pytest.mark.asyncio
async def test_speech_provider_command_switches(tmp_path: Path):
    bot = _build_bot(tmp_path, [])
    bot.speech = DualFakeSpeech(tmp_path / "v.ogg")
    await bot._handle_command("chat-a", "speech", "local")
    assert bot.state.get("chat-a").speech_provider == "local"
    assert bot._speech_provider_for("chat-a") == "local"
    assert "local" in bot.app.bot.sent[-1][1]
    await bot._handle_command("chat-a", "speech", "default")
    assert bot.state.get("chat-a").speech_provider is None
    assert bot._speech_provider_for("chat-a") == bot.config.speech_provider


@pytest.mark.asyncio
async def test_help_command_lists_commands(tmp_path: Path):
    bot = _build_bot(tmp_path, [])
    await bot._handle_command("chat-a", "help", "")
    text = bot.app.bot.sent[-1][1]
    assert text == HELP_TEXT
    for cmd in ("/help", "/reset", "/cd", "/cwd", "/cost", "/stop", "/photo",
                "/file", "/tts", "/speech", "/model", "/say"):
        assert cmd in text


@pytest.mark.asyncio
async def test_model_command_sets_and_resets(tmp_path: Path):
    bot = _build_bot(tmp_path, [])
    await bot._handle_command("chat-a", "model", "")
    assert "grok-4.6" in bot.app.bot.sent[-1][1]
    await bot._handle_command("chat-a", "model", "grok-4.5")
    assert bot.state.get("chat-a").grok_model == "grok-4.5"
    assert bot._model_for("chat-a") == "grok-4.5"
    await bot._handle_command("chat-a", "model", "default")
    assert bot.state.get("chat-a").grok_model is None
    assert bot._model_for("chat-a") == bot.config.grok_model


@pytest.mark.asyncio
async def test_run_turn_pins_model_on_runner(tmp_path: Path):
    ok = ScriptedRunner([
        RunnerEvent(kind="text", data={"text": "hi"}),
        RunnerEvent(kind="result", data={"cost_usd": 0.01, "session_id": "s1"}),
    ])
    bot = _build_bot(tmp_path, [ok])
    bot.state.set_grok_model("chat-a", "grok-4.6")
    await bot._run_turn("chat-a", "hello")
    assert ok.model == "grok-4.6"
