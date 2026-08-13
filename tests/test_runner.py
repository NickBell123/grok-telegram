import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest

from grok_telegram.runner import GrokRunner, RunnerEvent


def _fake_grok_script(tmp_path: Path, events: list[dict]) -> Path:
    """Write a python script that mimics `grok -p --output-format streaming-messages-json`."""
    script = tmp_path / "fake_grok.py"
    script.write_text(textwrap.dedent(f"""
        import json, sys, time
        for ev in {events!r}:
            print(json.dumps(ev), flush=True)
        """).strip())
    return script


@pytest.mark.asyncio
async def test_yields_session_id_text_and_result(tmp_path: Path):
    events = [
        {"type": "system", "subtype": "init", "session_id": "sess-1", "cwd": "/work/proj"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "result", "subtype": "success", "total_cost_usd": 0.0042, "session_id": "sess-1"},
    ]
    script = _fake_grok_script(tmp_path, events)
    runner = GrokRunner(grok_cmd=[sys.executable, str(script)])
    out = []
    async for ev in runner.run(prompt="hi", session_id=None, cwd=str(tmp_path)):
        out.append(ev)
    kinds = [e.kind for e in out]
    assert kinds == ["session", "text", "tool_use", "result"]
    assert out[0].data["session_id"] == "sess-1"
    assert out[1].data["text"] == "Hello"
    assert out[2].data == {"name": "Bash", "input": {"command": "ls"}}
    assert out[3].data["cost_usd"] == 0.0042


@pytest.mark.asyncio
async def test_passes_resume_flag(tmp_path: Path):
    """When session_id provided, --resume is in argv."""
    script = tmp_path / "echo_argv.py"
    script.write_text(textwrap.dedent("""
        import json, sys
        print(json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.0, "session_id": "x", "argv": sys.argv}), flush=True)
        """).strip())
    runner = GrokRunner(grok_cmd=[sys.executable, str(script)])
    out = []
    async for ev in runner.run(prompt="hi", session_id="abc", cwd="/tmp"):
        out.append(ev)
    # last event is result, with argv embedded
    argv = out[-1].data["raw"]["argv"]
    assert "--resume" in argv
    assert "abc" in argv


@pytest.mark.asyncio
async def test_passes_model_flag(tmp_path: Path):
    """When model is set, --model is in argv even on resume."""
    script = tmp_path / "echo_argv.py"
    script.write_text(textwrap.dedent("""
        import json, sys
        print(json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.0, "session_id": "x", "argv": sys.argv}), flush=True)
        """).strip())
    runner = GrokRunner(grok_cmd=[sys.executable, str(script)], model="grok-4.6")
    out = []
    async for ev in runner.run(prompt="hi", session_id="abc", cwd="/tmp"):
        out.append(ev)
    argv = out[-1].data["raw"]["argv"]
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "grok-4.6"
    assert "--resume" in argv


@pytest.mark.asyncio
async def test_nonzero_exit_yields_error_event(tmp_path: Path):
    """A crashing CLI surfaces its exit code and stderr instead of failing silently."""
    script = tmp_path / "crash.py"
    script.write_text(textwrap.dedent("""
        import json, sys
        print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}}), flush=True)
        sys.stderr.write("boom: credit balance too low\\n")
        sys.exit(3)
        """).strip())
    runner = GrokRunner(grok_cmd=[sys.executable, str(script)])
    out = [ev async for ev in runner.run(prompt="hi", session_id=None, cwd=str(tmp_path))]
    assert out[0].kind == "text"
    assert out[-1].kind == "error"
    assert out[-1].data["returncode"] == 3
    assert "credit balance too low" in out[-1].data["stderr"]


@pytest.mark.asyncio
async def test_early_close_reaps_child_and_does_not_raise(tmp_path: Path):
    """Abandoning the stream mid-turn must kill the child, not orphan or explode."""
    script = tmp_path / "slow.py"
    script.write_text(textwrap.dedent("""
        import json, sys, time
        print(json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}), flush=True)
        time.sleep(30)
        """).strip())
    runner = GrokRunner(grok_cmd=[sys.executable, str(script)])
    agen = runner.run(prompt="hi", session_id=None, cwd=str(tmp_path))
    first = await agen.__anext__()
    assert first.kind == "session"

    # Closing mid-iteration used to hit "async generator ignored GeneratorExit".
    await asyncio.wait_for(agen.aclose(), timeout=5)

    assert runner._proc is not None
    assert runner._proc.returncode is not None, "child process was left running"


@pytest.mark.asyncio
async def test_chatty_stderr_does_not_hang_the_turn(tmp_path: Path):
    """
    A child that writes more to stderr than the pipe buffer holds must still
    complete. Without a concurrent drain the child blocks on write and the
    turn never finishes.
    """
    script = tmp_path / "chatty.py"
    script.write_text(textwrap.dedent("""
        import json, sys
        sys.stderr.write("x" * (512 * 1024))
        sys.stderr.flush()
        print(json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.5, "session_id": "s1"}), flush=True)
        """).strip())
    runner = GrokRunner(grok_cmd=[sys.executable, str(script)])

    async def collect():
        return [ev async for ev in runner.run(prompt="hi", session_id=None, cwd=str(tmp_path))]

    out = await asyncio.wait_for(collect(), timeout=15)
    assert [e.kind for e in out] == ["result"]
    assert out[0].data["cost_usd"] == 0.5


@pytest.mark.asyncio
async def test_stderr_tail_is_kept_and_bounded(tmp_path: Path):
    """On failure we report the tail of stderr, capped so a noisy child can't eat memory."""
    script = tmp_path / "noisy_crash.py"
    script.write_text(textwrap.dedent("""
        import sys
        sys.stderr.write("y" * (256 * 1024))
        sys.stderr.write("FINAL: session limit reached\\n")
        sys.stderr.flush()
        sys.exit(2)
        """).strip())
    runner = GrokRunner(grok_cmd=[sys.executable, str(script)])
    out = await asyncio.wait_for(
        _collect(runner, tmp_path), timeout=15
    )
    assert out[-1].kind == "error"
    assert out[-1].data["returncode"] == 2
    stderr = out[-1].data["stderr"]
    assert "FINAL: session limit reached" in stderr, "tail of stderr should survive"
    assert len(stderr) <= 128 * 1024, "stderr buffer should stay bounded"


async def _collect(runner: GrokRunner, tmp_path: Path) -> list[RunnerEvent]:
    return [ev async for ev in runner.run(prompt="hi", session_id=None, cwd=str(tmp_path))]
