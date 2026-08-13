import json
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grok_telegram.bot import should_speak
from grok_telegram.speech import SpeechError, SpeechService, load_xai_token, text_for_speech


def test_text_for_speech_strips_tools_and_markdown():
    raw = (
        "Hello **world**\n"
        "> 🔧 run_terminal_command: ls\n"
        "See [docs](https://x.com) and `code`.\n"
        "\n\n✅ ($0.01)"
    )
    out = text_for_speech(raw, max_chars=500)
    assert "world" in out
    assert "docs" in out
    assert "code" in out
    assert "🔧" not in out
    assert "✅" not in out
    assert "0.01" not in out


def test_text_for_speech_empty():
    assert text_for_speech("") == ""
    assert text_for_speech("> 🔧 Bash: x\n✅") == ""


def test_text_for_speech_prefers_sentence_boundary():
    long = "First sentence is short. " + ("Second goes on and on with filler words " * 20)
    out = text_for_speech(long, max_chars=80)
    assert out.startswith("First sentence is short")
    assert len(out) <= 85


def test_should_speak_modes():
    assert should_speak("on", voice_input=False) is True
    assert should_speak("on", voice_input=True) is True
    assert should_speak("off", voice_input=True) is False
    assert should_speak("auto", voice_input=True) is True
    assert should_speak("auto", voice_input=False) is False


def test_load_xai_token_from_auth_file(tmp_path: Path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({
        "https://auth.x.ai::client": {
            "auth_mode": "oidc",
            "key": "test-oauth-token-abc",
        }
    }))
    assert load_xai_token(auth) == "test-oauth-token-abc"


def test_load_xai_token_prefers_explicit_key(tmp_path: Path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"x": {"key": "from-file"}}))
    assert load_xai_token(auth, env_api_key="from-env") == "from-env"


def test_load_xai_token_missing_raises(tmp_path: Path):
    with pytest.raises(SpeechError, match="no xAI credentials"):
        load_xai_token(tmp_path / "missing.json")


@pytest.mark.asyncio
async def test_transcribe_local_uses_whisper_script(tmp_path: Path):
    audio = tmp_path / "in.ogg"
    audio.write_bytes(b"fake-audio")
    script = tmp_path / "fake_whisper.py"
    script.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys
        from pathlib import Path
        audio = Path(sys.argv[1])
        out_dir = None
        for i, a in enumerate(sys.argv):
            if a == "--output_dir":
                out_dir = Path(sys.argv[i + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{audio.stem}.txt").write_text("hello from voice")
        """))
    script.chmod(0o755)
    svc = SpeechService(provider="local", whisper_bin=str(script), work_dir=tmp_path / "speech")
    text = await svc.transcribe(audio)
    assert text == "hello from voice"


@pytest.mark.asyncio
async def test_synthesize_local_pipeline(tmp_path: Path):
    edge = tmp_path / "edge-tts"
    edge.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys
        out = None
        args = sys.argv[1:]
        for i, a in enumerate(args):
            if a == "--write-media":
                out = args[i + 1]
        open(out, "wb").write(b"ID3fake-mp3")
        """))
    edge.chmod(0o755)
    ff = tmp_path / "ffmpeg"
    ff.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys
        out = sys.argv[-1]
        open(out, "wb").write(b"OggSfake-opus")
        """))
    ff.chmod(0o755)
    svc = SpeechService(
        provider="local",
        edge_tts_bin=str(edge),
        ffmpeg_bin=str(ff),
        work_dir=tmp_path / "speech",
    )
    path = await svc.synthesize("Hello there friend")
    assert path.is_file()
    assert path.read_bytes().startswith(b"OggS")


@pytest.mark.asyncio
async def test_synthesize_rejects_empty_after_clean():
    svc = SpeechService(provider="local", edge_tts_bin="/nonexistent", ffmpeg_bin="/nonexistent")
    with pytest.raises(SpeechError, match="nothing to speak"):
        await svc.synthesize("> 🔧 tool\n✅")


@pytest.mark.asyncio
async def test_grok_tts_calls_xai_and_ffmpeg(tmp_path: Path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"x": {"key": "tok"}}))
    ff = tmp_path / "ffmpeg"
    ff.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys
        open(sys.argv[-1], "wb").write(b"OggSfrom-grok")
        """))
    ff.chmod(0o755)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read = AsyncMock(return_value=b"ID3mp3bytes")
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    svc = SpeechService(
        provider="grok",
        auth_path=auth,
        ffmpeg_bin=str(ff),
        work_dir=tmp_path / "speech",
        grok_tts_voice="ara",
    )
    with patch("grok_telegram.speech.aiohttp.ClientSession", return_value=mock_session):
        path = await svc.synthesize("Hello from Grok voice")
    assert path.read_bytes().startswith(b"OggS")
    # Ensure Authorization was sent
    call_kwargs = mock_session.post.call_args
    assert call_kwargs is not None
    headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
    assert headers["Authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_grok_stt_parses_text(tmp_path: Path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"x": {"key": "tok"}}))
    audio = tmp_path / "in.ogg"
    audio.write_bytes(b"oggdata")

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.text = AsyncMock(return_value=json.dumps({"text": "  transcribed line  ", "duration": 1.2}))
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    svc = SpeechService(provider="grok", auth_path=auth, work_dir=tmp_path / "speech")
    with patch("grok_telegram.speech.aiohttp.ClientSession", return_value=mock_session):
        text = await svc.transcribe(audio)
    assert text == "transcribed line"
