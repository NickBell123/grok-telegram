import asyncio
import contextlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, filters

from grok_telegram.config import Config
from grok_telegram.log import JsonlLogger
from grok_telegram.media import MediaCandidate, MediaCollector, classify, resolve_existing
from grok_telegram.ratelimit import SlidingWindowLimiter
from grok_telegram.runner import GrokRunner, RunnerEvent
from grok_telegram.speech import SpeechError, SpeechService, text_for_speech
from grok_telegram.state import SPEECH_PROVIDERS, TTS_MODES, StateStore
from grok_telegram.stream import MessageSink, StreamRenderer

KNOWN_COMMANDS = {
    "help", "reset", "cd", "cwd", "cost", "stop", "photo", "file",
    "tts", "voice", "say", "speech", "provider", "model",
}

HELP_TEXT = (
    "Grok Telegram bridge — commands:\n"
    "\n"
    "/help — this list\n"
    "/reset — clear the conversation, start fresh\n"
    "/cd <path> — change working directory\n"
    "/cwd — show working directory\n"
    "/cost — cost of the last turn\n"
    "/stop — interrupt the in-flight turn\n"
    "/photo <path> — send a local image\n"
    "/file <path> — send a local file\n"
    "/tts [on|off|auto] — voice reply mode (default auto)\n"
    "/speech grok|local|default — STT/TTS engine (alias /provider)\n"
    "/model [id|default] — show or pin the Grok model\n"
    "/say <text> — speak without running Grok (alias /voice)\n"
    "\n"
    "Anything else is sent to Grok. Paths are relative to the chat cwd unless absolute."
)


def is_authorized(user_id: Optional[int], allowed_user_id: int) -> bool:
    return user_id is not None and user_id == allowed_user_id


def parse_command(text: str) -> tuple[Optional[str], str]:
    """Return (command_name | None, remaining_text)."""
    if not text.startswith("/"):
        return (None, text)
    rest = text[1:]
    # split on first whitespace; preserve everything after
    parts = rest.split(None, 1)
    if not parts:
        return (None, "")
    cmd = parts[0]
    # Telegram may send /cmd@BotName — strip the @suffix.
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    arg = parts[1] if len(parts) > 1 else ""
    if cmd not in KNOWN_COMMANDS:
        return (None, text)
    return (cmd, arg)


def should_speak(tts_mode: str, voice_input: bool) -> bool:
    if tts_mode == "on":
        return True
    if tts_mode == "auto" and voice_input:
        return True
    return False


@dataclass
class LastTurn:
    cost: float | None = None


# The CLI refuses --resume for a session it no longer has. The stored
# session id then poisons every subsequent turn, so treat it as recoverable.
# Grok emits things like: Session "…" not found / Failed to restore session
STALE_SESSION_MARKERS = (
    "not found",
    "Failed to restore session",
    "session get failed",
)


def is_stale_session_error(ev: RunnerEvent) -> bool:
    if ev.kind != "error":
        return False
    blob = str(ev.data.get("stderr", "")) + str(ev.data.get("raw", ""))
    return any(m in blob for m in STALE_SESSION_MARKERS)


@dataclass
class _Attempt:
    """What one pass over the runner produced."""

    captured_session: str | None
    cost: float | None = None
    exception: Exception | None = None
    errors: list[RunnerEvent] = field(default_factory=list)
    produced_output: bool = False
    media: list[MediaCandidate] = field(default_factory=list)
    assistant_text: str = ""

    @property
    def stale_session(self) -> bool:
        return any(is_stale_session_error(e) for e in self.errors)


class TelegramSink(MessageSink):
    """MessageSink backed by python-telegram-bot."""

    def __init__(self, app: Application, chat_id: str):
        self.app = app
        self.chat_id = chat_id

    async def send(self, text: str, parse_mode: Optional[str] = None) -> int:
        kwargs: dict = {"chat_id": self.chat_id, "text": text}
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        msg = await self.app.bot.send_message(**kwargs)
        return msg.message_id

    async def edit(self, message_id: int, text: str, parse_mode: Optional[str] = None) -> None:
        kwargs: dict = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
        }
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        try:
            await self.app.bot.edit_message_text(**kwargs)
        except Exception:
            # Edits can fail on identical text, bad markup, or rate limits.
            # If rich text failed, caller may retry plain; here we ignore.
            if parse_mode:
                raise
            pass

    async def send_photo(self, path: str, caption: str | None = None) -> int:
        with open(path, "rb") as f:
            msg = await self.app.bot.send_photo(
                chat_id=self.chat_id,
                photo=f,
                caption=caption,
            )
        return msg.message_id

    async def send_document(self, path: str, caption: str | None = None) -> int:
        with open(path, "rb") as f:
            msg = await self.app.bot.send_document(
                chat_id=self.chat_id,
                document=f,
                caption=caption,
                filename=Path(path).name,
            )
        return msg.message_id

    async def send_voice(self, path: str, caption: str | None = None) -> int:
        with open(path, "rb") as f:
            msg = await self.app.bot.send_voice(
                chat_id=self.chat_id,
                voice=f,
                caption=caption,
            )
        return msg.message_id

    async def send_html(self, text: str) -> int:
        """Send with HTML parse mode; fall back to plain on failure."""
        try:
            return await self.send(text, parse_mode=ParseMode.HTML)
        except Exception:
            return await self.send(text, parse_mode=None)


class Bot:
    def __init__(
        self,
        config: Config,
        state: StateStore,
        runner_factory: Callable[[], GrokRunner],
        logger: JsonlLogger,
        limiter: SlidingWindowLimiter,
        speech: Optional[SpeechService] = None,
    ):
        self.config = config
        self.state = state
        # A fresh runner per turn: each owns its own subprocess, so /stop in one
        # chat cannot terminate another chat's in-flight turn.
        self.runner_factory = runner_factory
        self.logger = logger
        self.limiter = limiter
        self.speech = speech
        self._in_flight: dict[str, asyncio.Lock] = {}
        self._last_turn: dict[str, LastTurn] = {}
        self._current_runner: dict[str, GrokRunner] = {}
        self.app: Application = ApplicationBuilder().token(config.telegram_bot_token).build()
        self.app.add_handler(MessageHandler(filters.TEXT, self._on_message))
        self.app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, self._on_voice))

    async def send(self, chat_id: str, text: str, parse_mode: Optional[str] = None) -> int:
        kwargs: dict = {"chat_id": chat_id, "text": text}
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        msg = await self.app.bot.send_message(**kwargs)
        return msg.message_id

    async def _reply(self, chat_id: str, text: str, *, html: bool = False) -> None:
        """Status/command reply; optional light HTML with plain fallback."""
        if html:
            try:
                await self.app.bot.send_message(
                    chat_id=chat_id, text=text, parse_mode=ParseMode.HTML
                )
                return
            except Exception:
                pass
        await self.app.bot.send_message(chat_id=chat_id, text=text)

    async def _typing_loop(self, chat_id: str, interval_s: float = 4.0) -> None:
        """
        Keep the Telegram "typing…" indicator alive.

        Chat actions expire after ~5s, so we refresh a bit sooner until cancelled.
        """
        try:
            while True:
                try:
                    await self.app.bot.send_chat_action(
                        chat_id=chat_id,
                        action=ChatAction.TYPING,
                    )
                except Exception:
                    # Network blips shouldn't kill the turn.
                    pass
                await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            raise

    @contextlib.asynccontextmanager
    async def _show_typing(self, chat_id: str) -> AsyncIterator[None]:
        task = asyncio.create_task(self._typing_loop(chat_id))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _lock_for(self, chat_id: str) -> asyncio.Lock:
        lock = self._in_flight.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._in_flight[chat_id] = lock
        return lock

    def _auth_gate(self, update: Update) -> Optional[tuple[str, int]]:
        """Return (chat_id, user_id) if authorized, else None after logging."""
        user = update.effective_user
        chat = update.effective_chat
        if not user or not chat:
            return None
        if not is_authorized(user.id, self.config.allowed_user_id):
            self.logger.write("drop", chat_id=str(chat.id), details={"reason": "unauthorized", "user_id": user.id})
            return None
        return str(chat.id), user.id

    async def _rate_check(self, chat_id: str) -> bool:
        if self.limiter.allow(chat_id):
            return True
        self.logger.write("drop", chat_id=chat_id, details={"reason": "rate_limited"})
        await self.app.bot.send_message(chat_id=chat_id, text="⛔ rate limit hit; try again later")
        return False

    async def _on_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        gated = self._auth_gate(update)
        if gated is None:
            return
        chat_id, _ = gated
        msg = update.effective_message
        if not msg or not msg.text:
            return
        if not await self._rate_check(chat_id):
            return
        self.logger.write("inbound", chat_id=chat_id, details={"text": msg.text})

        cmd, arg = parse_command(msg.text)
        if cmd is not None:
            await self._handle_command(chat_id, cmd, arg)
            return

        async with self._lock_for(chat_id):
            await self._run_turn(chat_id, msg.text, voice_input=False)

    async def _on_voice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        gated = self._auth_gate(update)
        if gated is None:
            return
        chat_id, _ = gated
        msg = update.effective_message
        if not msg:
            return
        if not self.config.stt_enabled or self.speech is None:
            await self.app.bot.send_message(chat_id=chat_id, text="⚠️ STT is disabled on this host")
            return
        if not await self._rate_check(chat_id):
            return

        tg_file = msg.voice or msg.audio or msg.video_note
        if tg_file is None:
            return

        async with self._lock_for(chat_id):
            await self._handle_voice_turn(chat_id, tg_file.file_id)

    async def _handle_voice_turn(self, chat_id: str, file_id: str) -> None:
        assert self.speech is not None
        provider = self._apply_speech_provider(chat_id)
        status = await self.app.bot.send_message(
            chat_id=chat_id,
            text=f"🎤 transcribing… ({provider})",
        )
        tmp_root = Path(self.config.state_dir) / "tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="in-voice-", dir=tmp_root))
        audio_path = work / "in.ogg"
        try:
            async with self._show_typing(chat_id):
                tg = await self.app.bot.get_file(file_id)
                await tg.download_to_drive(custom_path=str(audio_path))
                transcript = await self.speech.transcribe(audio_path)
            self.logger.write("stt", chat_id=chat_id, details={"text": transcript})
            try:
                await self.app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status.message_id,
                    text=f"🎤 you said:\n{transcript}",
                )
            except Exception:
                await self.app.bot.send_message(chat_id=chat_id, text=f"🎤 you said:\n{transcript}")
            # _run_turn has its own typing indicator for the agent phase.
            await self._run_turn(chat_id, transcript, voice_input=True)
        except SpeechError as e:
            self.logger.write("stt", chat_id=chat_id, details={"error": str(e)})
            try:
                await self.app.bot.edit_message_text(
                    chat_id=chat_id, message_id=status.message_id, text=f"⚠️ STT failed: {e}"
                )
            except Exception:
                await self.app.bot.send_message(chat_id=chat_id, text=f"⚠️ STT failed: {e}")
        except Exception as e:
            self.logger.write("stt", chat_id=chat_id, details={"error": f"{type(e).__name__}: {e}"})
            try:
                await self.app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status.message_id,
                    text=f"⚠️ STT failed: {type(e).__name__}: {e}",
                )
            except Exception:
                pass
        finally:
            # Best-effort cleanup of downloaded audio.
            try:
                for p in work.iterdir():
                    p.unlink(missing_ok=True)
                work.rmdir()
            except Exception:
                pass

    async def _handle_command(self, chat_id: str, cmd: str, arg: str) -> None:
        if cmd == "help":
            await self.app.bot.send_message(chat_id=chat_id, text=HELP_TEXT)
        elif cmd == "reset":
            self.state.clear_session(chat_id)
            await self.app.bot.send_message(chat_id=chat_id, text="🔄 session cleared")
        elif cmd == "cd":
            if not arg:
                await self.app.bot.send_message(chat_id=chat_id, text="usage: /cd <path>")
                return
            if "\n" in arg or "\r" in arg:
                await self.app.bot.send_message(chat_id=chat_id, text="send one command per message")
                return
            # Resolve like a shell cd: relative to this chat's cwd, not to the
            # process working directory. Validate before persisting — an
            # unchecked path is written to state.json and then fails every
            # later turn at subprocess spawn.
            current = self.state.get(chat_id).cwd
            cwd = os.path.abspath(os.path.join(current, os.path.expanduser(arg)))
            if not os.path.isdir(cwd):
                await self.app.bot.send_message(chat_id=chat_id, text=f"⚠️ not a directory: {cwd}")
                return
            self.state.set_cwd(chat_id, cwd)
            await self.app.bot.send_message(chat_id=chat_id, text=f"📁 cwd set to {cwd}")
        elif cmd == "cwd":
            s = self.state.get(chat_id)
            await self.app.bot.send_message(chat_id=chat_id, text=f"📁 {s.cwd}")
        elif cmd == "cost":
            last = self._last_turn.get(chat_id)
            if last and last.cost is not None:
                await self.app.bot.send_message(chat_id=chat_id, text=f"💰 last turn: ${last.cost:.4f}")
            else:
                await self.app.bot.send_message(chat_id=chat_id, text="no completed turn yet")
        elif cmd == "stop":
            r = self._current_runner.get(chat_id)
            if r:
                r.terminate()
                await self.app.bot.send_message(chat_id=chat_id, text="🛑 stopping")
            else:
                await self.app.bot.send_message(chat_id=chat_id, text="nothing running")
        elif cmd in ("photo", "file"):
            await self._send_path_command(chat_id, cmd, arg)
        elif cmd == "tts":
            await self._tts_command(chat_id, arg)
        elif cmd in ("speech", "provider"):
            await self._speech_provider_command(chat_id, arg)
        elif cmd in ("voice", "say"):
            await self._say_command(chat_id, arg)
        elif cmd == "model":
            await self._model_command(chat_id, arg)

    def _model_for(self, chat_id: str) -> str:
        """Active grok --model for this chat (per-chat override or config default)."""
        override = self.state.get(chat_id).grok_model
        if override:
            return override
        return self.config.grok_model

    async def _model_command(self, chat_id: str, arg: str) -> None:
        arg = (arg or "").strip()
        if not arg or arg.lower() in ("status", "show"):
            model = self._model_for(chat_id)
            override = self.state.get(chat_id).grok_model
            src = "chat override" if override else f"default ({self.config.grok_model})"
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🧠 model: {model} ({src})\n\n"
                    "usage: /model <id> | default\n"
                    "examples: /model grok-4.6 · /model grok-4.5 · /model default"
                ),
            )
            return
        if arg.lower() in ("default", "reset", "config"):
            self.state.set_grok_model(chat_id, None)
            model = self._model_for(chat_id)
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=f"🧠 model reset to default ({model})",
            )
            self.logger.write("model", chat_id=chat_id, details={"model": model, "source": "default"})
            return
        if any(ch.isspace() for ch in arg) or arg.startswith("-"):
            await self.app.bot.send_message(
                chat_id=chat_id,
                text="usage: /model <id> | default\nexample: /model grok-4.6",
            )
            return
        self.state.set_grok_model(chat_id, arg)
        await self.app.bot.send_message(chat_id=chat_id, text=f"🧠 model set to {arg}")
        self.logger.write("model", chat_id=chat_id, details={"model": arg})

    def _speech_provider_for(self, chat_id: str) -> str:
        """Active STT/TTS backend for this chat (per-chat override or config default)."""
        override = self.state.get(chat_id).speech_provider
        if override in SPEECH_PROVIDERS:
            return override
        return self.config.speech_provider

    def _apply_speech_provider(self, chat_id: str) -> str:
        """Point SpeechService at this chat's provider; return the active name."""
        provider = self._speech_provider_for(chat_id)
        if self.speech is not None:
            self.speech.provider = provider
        return provider

    def _provider_label(self, provider: str) -> tuple[str, str]:
        if provider == "grok":
            return self.config.grok_tts_voice, "Grok Voice API (subscription OAuth)"
        return (
            self.config.edge_tts_voice,
            f"local Whisper/{self.config.whisper_model} + edge-tts",
        )

    async def _tts_command(self, chat_id: str, arg: str) -> None:
        arg = (arg or "").strip().lower()
        if not arg:
            mode = self.state.get(chat_id).tts_mode
            provider = self._apply_speech_provider(chat_id)
            avail = self.speech.available() if self.speech else {"stt": False, "tts": False}
            voice, backend = self._provider_label(provider)
            default = self.config.speech_provider
            override = self.state.get(chat_id).speech_provider
            src = f"chat override" if override else f"default ({default})"
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🔊 TTS mode: {mode}\n"
                    f"provider: {provider} ({src}) — {backend}\n"
                    f"STT: {'on' if self.config.stt_enabled and avail.get('stt') else 'off'} · "
                    f"TTS: {'on' if self.config.tts_enabled and avail.get('tts') else 'off'}\n"
                    f"voice: {voice}\n\n"
                    "usage: /tts on | off | auto\n"
                    "· on — speak every text reply\n"
                    "· auto — speak when you send a voice note\n"
                    "· off — text only\n"
                    "/speech grok | local — switch STT/TTS engine\n"
                    "/say <text> — speak without running Grok"
                ),
            )
            return
        if arg not in TTS_MODES:
            await self.app.bot.send_message(chat_id=chat_id, text="usage: /tts on | off | auto")
            return
        self.state.set_tts_mode(chat_id, arg)
        await self.app.bot.send_message(chat_id=chat_id, text=f"🔊 TTS mode set to {arg}")

    async def _speech_provider_command(self, chat_id: str, arg: str) -> None:
        arg = (arg or "").strip().lower()
        if not arg or arg in ("status", "show"):
            provider = self._apply_speech_provider(chat_id)
            override = self.state.get(chat_id).speech_provider
            voice, backend = self._provider_label(provider)
            # Probe both backends so the user knows what's available.
            grok_ok = local_ok = False
            if self.speech is not None:
                prev = self.speech.provider
                self.speech.provider = "grok"
                grok_ok = bool(self.speech.available().get("stt") and self.speech.available().get("tts"))
                # available() for grok: stt and tts may differ if ffmpeg missing
                g = self.speech.available()
                grok_ok = bool(g.get("stt") and g.get("tts"))
                self.speech.provider = "local"
                loc = self.speech.available()
                local_ok = bool(loc.get("stt") and loc.get("tts"))
                self.speech.provider = prev
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🎙️ Speech provider: {provider}"
                    f"{' (chat override)' if override else ' (default)'}\n"
                    f"  grok  — {self._provider_label('grok')[1]} "
                    f"[{'ready' if grok_ok else 'unavailable'}]\n"
                    f"  local — {self._provider_label('local')[1]} "
                    f"[{'ready' if local_ok else 'unavailable'}]\n"
                    f"voice when active: {voice}\n\n"
                    "usage: /speech grok | local | default\n"
                    "alias: /provider …"
                ),
            )
            return
        if arg in ("default", "reset", "config"):
            self.state.set_speech_provider(chat_id, None)
            provider = self._apply_speech_provider(chat_id)
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=f"🎙️ speech provider reset to default ({provider})",
            )
            self.logger.write("speech_provider", chat_id=chat_id, details={"provider": provider, "source": "default"})
            return
        if arg not in SPEECH_PROVIDERS:
            await self.app.bot.send_message(
                chat_id=chat_id,
                text="usage: /speech grok | local | default",
            )
            return
        # Validate the chosen backend is usable before persisting.
        if self.speech is not None:
            prev = self.speech.provider
            self.speech.provider = arg
            avail = self.speech.available()
            self.speech.provider = prev
            if not avail.get("stt") and not avail.get("tts"):
                await self.app.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ {arg} provider is not available on this host (check binaries / login)",
                )
                return
            missing = []
            if self.config.stt_enabled and not avail.get("stt"):
                missing.append("STT")
            if self.config.tts_enabled and not avail.get("tts"):
                missing.append("TTS")
            if missing:
                await self.app.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ {arg} provider incomplete: missing {', '.join(missing)}. Switching anyway.",
                )
        self.state.set_speech_provider(chat_id, arg)
        self._apply_speech_provider(chat_id)
        voice, backend = self._provider_label(arg)
        await self.app.bot.send_message(
            chat_id=chat_id,
            text=f"🎙️ speech provider set to {arg}\n{backend}\nvoice: {voice}",
        )
        self.logger.write("speech_provider", chat_id=chat_id, details={"provider": arg})

    async def _say_command(self, chat_id: str, arg: str) -> None:
        if not arg.strip():
            await self.app.bot.send_message(chat_id=chat_id, text="usage: /say <text to speak>")
            return
        if not self.config.tts_enabled or self.speech is None:
            await self.app.bot.send_message(chat_id=chat_id, text="⚠️ TTS is disabled on this host")
            return
        self._apply_speech_provider(chat_id)
        sink = TelegramSink(self.app, chat_id)
        try:
            # Explicit /say: allow longer speech than auto-replies.
            path = await self.speech.synthesize(arg, max_chars=max(self.config.speech_max_chars, 1200))
            try:
                await sink.send_voice(str(path))
                self.logger.write(
                    "tts",
                    chat_id=chat_id,
                    details={
                        "cmd": "say",
                        "chars": len(arg),
                        "provider": self._speech_provider_for(chat_id),
                    },
                )
            finally:
                Path(path).unlink(missing_ok=True)
        except SpeechError as e:
            await self.app.bot.send_message(chat_id=chat_id, text=f"⚠️ TTS failed: {e}")
        except Exception as e:
            await self.app.bot.send_message(chat_id=chat_id, text=f"⚠️ TTS failed: {type(e).__name__}: {e}")

    async def _send_path_command(self, chat_id: str, cmd: str, arg: str) -> None:
        if not arg:
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=f"usage: /{cmd} <path>\npath is relative to this chat's cwd unless absolute",
            )
            return
        if "\n" in arg or "\r" in arg:
            await self.app.bot.send_message(chat_id=chat_id, text="send one path per message")
            return
        cwd = self.state.get(chat_id).cwd
        resolved = resolve_existing(arg, cwd)
        if resolved is None:
            await self.app.bot.send_message(chat_id=chat_id, text=f"⚠️ not a readable file: {arg}")
            return
        force = "photo" if cmd == "photo" else "document"
        cand = classify(resolved, force=force)
        if cand is None:
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ cannot send {resolved.name} (missing, denied, or over size limit)",
            )
            return
        sink = TelegramSink(self.app, chat_id)
        try:
            await self._dispatch_media(sink, [cand])
            self.logger.write("media", chat_id=chat_id, details={"cmd": cmd, "path": str(resolved), "kind": cand.kind})
        except Exception as e:
            await self.app.bot.send_message(chat_id=chat_id, text=f"⚠️ send failed: {type(e).__name__}: {e}")

    async def _dispatch_media(self, sink: MessageSink, items: list[MediaCandidate]) -> None:
        for item in items:
            caption = item.path.name
            if item.kind == "photo":
                await sink.send_photo(str(item.path), caption=caption)
            else:
                await sink.send_document(str(item.path), caption=caption)

    async def _maybe_speak(self, chat_id: str, sink: MessageSink, text: str, voice_input: bool) -> None:
        if not self.config.tts_enabled or self.speech is None:
            return
        mode = self.state.get(chat_id).tts_mode
        if not should_speak(mode, voice_input):
            return
        provider = self._apply_speech_provider(chat_id)
        spoken = text_for_speech(text, max_chars=self.config.speech_max_chars)
        if not spoken:
            return
        try:
            path = await self.speech.synthesize(spoken, max_chars=self.config.speech_max_chars)
            try:
                await sink.send_voice(str(path))
                self.logger.write(
                    "tts",
                    chat_id=chat_id,
                    details={
                        "chars": len(spoken),
                        "mode": mode,
                        "voice_input": voice_input,
                        "max_chars": self.config.speech_max_chars,
                        "provider": provider,
                    },
                )
            finally:
                Path(path).unlink(missing_ok=True)
        except Exception as e:
            self.logger.write("tts", chat_id=chat_id, details={"error": f"{type(e).__name__}: {e}", "provider": provider})
            try:
                await sink.send(f"⚠️ TTS failed: {type(e).__name__}: {e}")
            except Exception:
                pass

    async def _run_turn(self, chat_id: str, text: str, voice_input: bool = False) -> None:
        state = self.state.get(chat_id)
        sink = TelegramSink(self.app, chat_id)
        renderer = StreamRenderer(
            sink,
            max_chars=self.config.max_telegram_chars,
            min_edit_interval_s=self.config.edit_min_interval_s,
        )

        async with self._show_typing(chat_id):
            await renderer.start_placeholder()

            session_id = state.session_id
            attempt = await self._stream_attempt(chat_id, renderer, text, session_id, state.cwd)
            # One retry from a clean session when the CLI rejected a session id that
            # no longer exists — but only while the user has seen nothing, so the
            # retry cannot duplicate output that was already rendered.
            if attempt.stale_session and session_id is not None and not attempt.produced_output:
                self.logger.write("session_reset", chat_id=chat_id, details={"stale_session_id": session_id})
                self.state.clear_session(chat_id)
                session_id = None
                attempt = await self._stream_attempt(chat_id, renderer, text, None, state.cwd)

            error = attempt.exception
            failed = error is not None or bool(attempt.errors)
            if error is not None:
                try:
                    await sink.send(f"⚠️ runner error: {type(error).__name__}: {error}")
                except Exception:
                    pass
            else:
                for ev in attempt.errors:
                    await renderer.handle(ev)
                try:
                    await renderer.finalize(ok=not failed)
                except Exception:
                    pass

            # Ship media and TTS concurrently so speech doesn't wait on photo uploads.
            post: list = []
            if attempt.media and not failed:
                async def _media() -> None:
                    try:
                        await self._dispatch_media(sink, attempt.media)
                        self.logger.write(
                            "media",
                            chat_id=chat_id,
                            details={"paths": [str(m.path) for m in attempt.media], "count": len(attempt.media)},
                        )
                    except Exception as e:
                        try:
                            await sink.send(f"⚠️ media send failed: {type(e).__name__}: {e}")
                        except Exception:
                            pass
                post.append(_media())
            if not failed and attempt.assistant_text:
                post.append(self._maybe_speak(chat_id, sink, attempt.assistant_text, voice_input=voice_input))
            if post:
                await asyncio.gather(*post)

        captured_session = attempt.captured_session
        if captured_session and captured_session != self.state.get(chat_id).session_id:
            self.state.set_session(chat_id, captured_session)
        self._last_turn[chat_id] = LastTurn(cost=attempt.cost)
        self.logger.write(
            "grok",
            chat_id=chat_id,
            details={
                "cost_usd": attempt.cost,
                "session_id": captured_session,
                "error": f"{type(error).__name__}: {error}" if error else None,
                "voice_input": voice_input,
            },
        )

    async def _stream_attempt(
        self,
        chat_id: str,
        renderer: StreamRenderer,
        text: str,
        session_id: Optional[str],
        cwd: str,
    ) -> _Attempt:
        """Run the prompt once, rendering as it goes. Error events are held back
        so a recoverable failure can be retried without the user ever seeing it."""
        runner = self.runner_factory()
        runner.model = self._model_for(chat_id)
        self._current_runner[chat_id] = runner
        out = _Attempt(captured_session=session_id)
        collector = MediaCollector(cwd)
        text_parts: list[str] = []
        try:
            async for ev in runner.run(prompt=text, session_id=session_id, cwd=cwd):
                if ev.kind == "session" and ev.data.get("session_id"):
                    out.captured_session = ev.data["session_id"]
                if ev.kind == "result":
                    out.cost = ev.data.get("cost_usd")
                    if ev.data.get("session_id"):
                        out.captured_session = ev.data["session_id"]
                if ev.kind == "error":
                    out.errors.append(ev)
                    continue
                if ev.kind == "tool_use":
                    out.produced_output = True
                    collector.note_tool_use(ev.data.get("name", ""), ev.data.get("input") or {})
                if ev.kind == "tool_result":
                    collector.note_tool_result(ev.data.get("content"))
                if ev.kind == "text":
                    out.produced_output = True
                    chunk = ev.data.get("text", "") or ""
                    text_parts.append(chunk)
                    collector.note_text(chunk)
                await renderer.handle(ev)
        except Exception as e:
            out.exception = e
        finally:
            self._current_runner.pop(chat_id, None)
        out.media = collector.candidates()
        out.assistant_text = "".join(text_parts)
        return out
