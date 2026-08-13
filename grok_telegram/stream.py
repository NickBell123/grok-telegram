import time
from typing import Callable, Optional, Protocol

from grok_telegram.richtext import safe_html
from grok_telegram.runner import RunnerEvent


class MessageSink(Protocol):
    async def send(self, text: str, parse_mode: Optional[str] = None) -> int: ...
    async def edit(self, message_id: int, text: str, parse_mode: Optional[str] = None) -> None: ...
    async def send_photo(self, path: str, caption: str | None = None) -> int: ...
    async def send_document(self, path: str, caption: str | None = None) -> int: ...
    async def send_voice(self, path: str, caption: str | None = None) -> int: ...


PLACEHOLDER = "⚡ thinking…"


class StreamRenderer:
    """
    Buffers runner events and renders them to Telegram by editing one message,
    respecting Telegram's edit-rate limit and per-message char limit.

    Streaming updates are plain text (partial markdown is unsafe to parse).
    On finalize / segment split, the buffer is converted to Telegram HTML.
    """

    def __init__(
        self,
        sink: MessageSink,
        max_chars: int = 3900,
        min_edit_interval_s: float = 1.2,
        now_fn: Callable[[], float] = time.monotonic,
        rich_text: bool = True,
    ):
        self.sink = sink
        self.max_chars = max_chars
        self.min_edit_interval_s = min_edit_interval_s
        self.now_fn = now_fn
        self.rich_text = rich_text
        self._current_msg_id: int | None = None
        self._current_text: str = ""
        self._last_edit_at: float = 0.0
        self._final_cost: float | None = None

    async def start_placeholder(self) -> None:
        self._current_msg_id = await self.sink.send(PLACEHOLDER)
        self._current_text = ""
        self._last_edit_at = self.now_fn()

    async def handle(self, ev: RunnerEvent) -> None:
        addition = self._format(ev)
        if not addition:
            if ev.kind == "result":
                self._final_cost = ev.data.get("cost_usd")
            return
        # If adding would overflow, finalize current message and start a fresh one.
        if len(self._current_text) + len(addition) > self.max_chars:
            await self._flush(rich=self.rich_text)
            self._current_msg_id = await self.sink.send(PLACEHOLDER)
            self._current_text = ""
            self._last_edit_at = self.now_fn()
        self._current_text += addition
        # Throttled edit (plain while streaming).
        if self.now_fn() - self._last_edit_at >= self.min_edit_interval_s:
            await self._flush(rich=False)

    async def finalize(self, ok: bool = True) -> None:
        if ok:
            suffix = "\n\n✅"
            if self._final_cost is not None:
                suffix += f" (${self._final_cost:.4f})"
        else:
            # A turn that errored must never carry a success tick.
            suffix = "\n\n❌"
        self._current_text += suffix
        await self._flush(rich=self.rich_text)

    async def _flush(self, rich: bool = False) -> None:
        """Write the buffer to Telegram. Callers decide when; throttling lives at the call site."""
        if self._current_msg_id is None:
            return
        plain = self._current_text or PLACEHOLDER
        if rich and plain != PLACEHOLDER:
            formatted = safe_html(plain)
            try:
                await self.sink.edit(self._current_msg_id, formatted, parse_mode="HTML")
                self._last_edit_at = self.now_fn()
                return
            except Exception:
                # Fall back to plain if Telegram rejects the markup.
                pass
        await self.sink.edit(self._current_msg_id, plain, parse_mode=None)
        self._last_edit_at = self.now_fn()

    def _format(self, ev: RunnerEvent) -> str:
        if ev.kind == "text":
            return ev.data.get("text", "")
        if ev.kind == "tool_use":
            name = ev.data.get("name", "?")
            inp = ev.data.get("input", {})
            # Compact one-line render. Shell tools get their command; else short JSON.
            if name in ("Bash", "run_terminal_command", "Shell") and "command" in inp:
                detail = inp["command"]
            else:
                detail = ", ".join(f"{k}={v!r}" for k, v in list(inp.items())[:2])
            return f"\n> 🔧 {name}: {detail}\n"
        if ev.kind == "tool_result":
            return ""  # too noisy to render; rely on next assistant text
        if ev.kind == "error":
            # Prefer the child's own words; the raw event dict is noise on a phone.
            detail = ev.data.get("stderr") or ev.data.get("raw") or ev.data
            return f"\n⚠️ error: {str(detail).strip()}\n"
        return ""
