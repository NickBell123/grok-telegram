import asyncio
from pathlib import Path

import pytest

from grok_telegram.bot import Bot
from grok_telegram.config import Config
from grok_telegram.log import JsonlLogger
from grok_telegram.ratelimit import SlidingWindowLimiter
from grok_telegram.runner import RunnerEvent
from grok_telegram.state import StateStore

FAKE_TOKEN = "123456:AAHfakefakefakefakefakefakefakefake"


class FakeBotApi:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self._id = 0

    async def send_message(self, chat_id: str, text: str, parse_mode: str | None = None):
        self._id += 1
        self.sent.append((chat_id, text))
        return type("Msg", (), {"message_id": self._id})()

    async def edit_message_text(
        self, chat_id: str, message_id: int, text: str, parse_mode: str | None = None
    ) -> None:
        return None

    async def send_chat_action(self, chat_id: str, action) -> None:
        return None

    async def send_photo(self, chat_id: str, photo, caption: str | None = None):
        self._id += 1
        return type("Msg", (), {"message_id": self._id})()

    async def send_document(self, chat_id: str, document, caption: str | None = None, filename: str | None = None):
        self._id += 1
        return type("Msg", (), {"message_id": self._id})()

    async def send_voice(self, chat_id: str, voice, caption: str | None = None):
        self._id += 1
        return type("Msg", (), {"message_id": self._id})()


class FakeApp:
    def __init__(self):
        self.bot = FakeBotApi()


class FakeRunner:
    """Yields one chunk, then blocks until released or terminated."""

    def __init__(self, name: str):
        self.name = name
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.terminated = False

    async def run(self, prompt: str, session_id, cwd: str):
        yield RunnerEvent(kind="session", data={"session_id": f"sess-{self.name}"})
        yield RunnerEvent(kind="text", data={"text": f"working in {self.name}"})
        self.started.set()
        await self.release.wait()
        yield RunnerEvent(kind="result", data={"cost_usd": 0.0, "session_id": f"sess-{self.name}"})

    def terminate(self) -> None:
        self.terminated = True
        self.release.set()


def _build_bot(tmp_path: Path, runners: list[FakeRunner]) -> Bot:
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


@pytest.mark.asyncio
async def test_stop_terminates_only_the_calling_chats_turn(tmp_path: Path):
    a, b = FakeRunner("chat-a"), FakeRunner("chat-b")
    bot = _build_bot(tmp_path, [a, b])

    turn_a = asyncio.create_task(bot._run_turn("chat-a", "hello"))
    turn_b = asyncio.create_task(bot._run_turn("chat-b", "hello"))
    await asyncio.wait_for(a.started.wait(), timeout=2)
    await asyncio.wait_for(b.started.wait(), timeout=2)

    await bot._handle_command("chat-a", "stop", "")

    assert a.terminated is True, "/stop in chat-a should terminate chat-a's turn"
    assert b.terminated is False, "/stop in chat-a must not terminate chat-b's turn"

    b.release.set()
    await asyncio.wait_for(asyncio.gather(turn_a, turn_b), timeout=2)


@pytest.mark.asyncio
async def test_each_turn_gets_its_own_runner(tmp_path: Path):
    a, b = FakeRunner("chat-a"), FakeRunner("chat-b")
    bot = _build_bot(tmp_path, [a, b])

    turn_a = asyncio.create_task(bot._run_turn("chat-a", "hello"))
    await asyncio.wait_for(a.started.wait(), timeout=2)
    turn_b = asyncio.create_task(bot._run_turn("chat-b", "hello"))
    await asyncio.wait_for(b.started.wait(), timeout=2)

    assert bot._current_runner["chat-a"] is a
    assert bot._current_runner["chat-b"] is b

    a.release.set()
    b.release.set()
    await asyncio.wait_for(asyncio.gather(turn_a, turn_b), timeout=2)

    # Both cleared once their turn finished.
    assert bot._current_runner == {}


@pytest.mark.asyncio
async def test_cd_rejects_multiline_argument_without_changing_cwd(tmp_path: Path):
    bot = _build_bot(tmp_path, [])
    original_cwd = bot.state.get("chat-a").cwd

    await bot._handle_command("chat-a", "cd", "~/grok-telegram\n/cwd")

    assert bot.state.get("chat-a").cwd == original_cwd
    assert bot.app.bot.sent[-1][1] == "send one command per message"


@pytest.mark.asyncio
async def test_cd_expands_home_directory(tmp_path: Path, monkeypatch):
    bot = _build_bot(tmp_path, [])
    project = tmp_path / "grok-telegram"
    project.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))

    await bot._handle_command("chat-a", "cd", "~/grok-telegram")

    assert bot.state.get("chat-a").cwd == str(project)
    assert bot.app.bot.sent[-1][1] == f"📁 cwd set to {project}"


@pytest.mark.asyncio
async def test_stop_with_nothing_running_is_a_noop(tmp_path: Path):
    bot = _build_bot(tmp_path, [])
    await bot._handle_command("chat-a", "stop", "")
    assert bot.app.bot.sent[-1][1] == "nothing running"
