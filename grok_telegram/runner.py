import asyncio
import json
from dataclasses import dataclass
from typing import AsyncIterator, Optional


@dataclass
class RunnerEvent:
    kind: str  # "session" | "text" | "tool_use" | "tool_result" | "result" | "error"
    data: dict


# Keep the tail of stderr for error reporting without growing without bound.
MAX_STDERR_BYTES = 64 * 1024


async def _drain_stderr(stream: asyncio.StreamReader, sink: list[bytes]) -> None:
    """
    Read stderr continuously so the child never blocks on a full pipe.

    Reading must not stop once the cap is reached — that is what causes the
    hang — so keep consuming and discard the oldest chunks instead.
    """
    total = 0
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return
        sink.append(chunk)
        total += len(chunk)
        while total > MAX_STDERR_BYTES and len(sink) > 1:
            total -= len(sink.pop(0))


class GrokRunner:
    """
    Spawns the grok CLI and yields normalized events from streaming-messages-json.

    One instance owns at most one subprocess, so `terminate()` can only ever kill
    the turn this instance started. Create a fresh runner per turn — see
    `Bot._run_turn` — rather than sharing one across concurrent chats.
    """

    def __init__(self, grok_cmd: list[str] | None = None, model: str | None = None):
        # Allow override for tests; default is the real CLI.
        self.grok_cmd = grok_cmd or ["grok"]
        self.model = model
        self._proc: asyncio.subprocess.Process | None = None

    def _build_argv(self, prompt: str, session_id: Optional[str]) -> list[str]:
        # streaming-messages-json matches Claude Code's stream-json shape closely
        # (system/init, assistant content blocks, result with total_cost_usd).
        argv = list(self.grok_cmd) + [
            "-p", prompt,
            "--output-format", "streaming-messages-json",
            "--always-approve",
        ]
        if self.model:
            argv += ["--model", self.model]
        if session_id:
            argv += ["--resume", session_id]
        return argv

    async def run(
        self,
        prompt: str,
        session_id: Optional[str],
        cwd: str,
    ) -> AsyncIterator[RunnerEvent]:
        argv = self._build_argv(prompt, session_id)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._proc = proc
        # Drain stderr in the background for the whole life of the process. If
        # nothing reads it, a chatty child fills the pipe buffer, blocks on
        # write, and the turn hangs forever with no output and no error.
        stderr_chunks: list[bytes] = []
        stderr_task = (
            asyncio.create_task(_drain_stderr(proc.stderr, stderr_chunks))
            if proc.stderr is not None
            else None
        )
        try:
            assert proc.stdout is not None
            async for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    yield RunnerEvent(kind="error", data={"raw": line.decode("utf-8", "replace")})
                    continue
                for normalized in self._normalize(ev):
                    yield normalized
        except BaseException:
            # The consumer broke out, raised, or was cancelled (GeneratorExit).
            # Reap the child so we don't orphan a grok process — but never
            # yield during cleanup: a yield here would mask the real exception
            # with "async generator ignored GeneratorExit".
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            if stderr_task is not None:
                stderr_task.cancel()
            raise

        # Normal completion: stdout hit EOF, so it is safe to yield again.
        rc = await proc.wait()
        if stderr_task is not None:
            await stderr_task
        if rc != 0:
            err = b"".join(stderr_chunks).decode("utf-8", "replace")
            yield RunnerEvent(kind="error", data={"returncode": rc, "stderr": err})

    def _normalize(self, ev: dict) -> list[RunnerEvent]:
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            return [RunnerEvent(kind="session", data={"session_id": ev.get("session_id"), "cwd": ev.get("cwd")})]
        if t == "assistant":
            out = []
            for block in ev.get("message", {}).get("content", []) or []:
                btype = block.get("type")
                if btype == "text":
                    out.append(RunnerEvent(kind="text", data={"text": block.get("text", "")}))
                elif btype == "tool_use":
                    out.append(RunnerEvent(kind="tool_use", data={"name": block.get("name"), "input": block.get("input", {})}))
                # thinking blocks are intentionally dropped — too noisy on a phone
            return out
        if t == "user":
            out = []
            for block in ev.get("message", {}).get("content", []) or []:
                if block.get("type") == "tool_result":
                    out.append(RunnerEvent(kind="tool_result", data={"content": block.get("content"), "is_error": block.get("is_error", False)}))
            return out
        if t == "result":
            return [RunnerEvent(kind="result", data={
                "cost_usd": ev.get("total_cost_usd", 0.0),
                "session_id": ev.get("session_id"),
                "raw": ev,
            })]
        return []

    def terminate(self) -> None:
        proc = self._proc
        if proc and proc.returncode is None:
            proc.terminate()
