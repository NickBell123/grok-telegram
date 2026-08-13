"""STT/TTS for Telegram voice — Grok (xAI) by default, local Whisper/edge-tts fallback."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

import aiohttp


class SpeechError(Exception):
    pass


# Strip markdown-ish noise so speech sounds natural.
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_CODE_FENCE = re.compile(r"```[\s\S]*?```", re.MULTILINE)
_MD_INLINE_CODE = re.compile(r"`([^`]+)`")
_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")
_MD_ITALIC = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)|(?<!_)_([^_]+)_(?!_)")
_TOOL_LINE = re.compile(r"^>\s*🔧.*$", re.MULTILINE)
_EMOJI_HEAVY = re.compile(r"[⚡✅❌🛑📁💰🔄🎤🔊⚠️⛔]+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

XAI_TTS_URL = "https://api.x.ai/v1/tts"
XAI_STT_URL = "https://api.x.ai/v1/stt"
DEFAULT_AUTH_PATH = Path.home() / ".grok" / "auth.json"

# Grok TTS latency scales roughly with text length (~1.5–3s per 100 chars).
# Keep spoken replies short by default so voice feels snappy; full text stays on screen.
DEFAULT_SPEECH_MAX_CHARS = 280


def text_for_speech(text: str, max_chars: int = DEFAULT_SPEECH_MAX_CHARS) -> str:
    """Clean assistant reply text for TTS; prefer full sentences under max_chars."""
    if not text:
        return ""
    t = text
    t = _TOOL_LINE.sub("", t)
    t = _MD_CODE_FENCE.sub(" ", t)
    t = _MD_LINK.sub(r"\1", t)
    t = _MD_INLINE_CODE.sub(r"\1", t)
    t = _MD_BOLD.sub(lambda m: m.group(1) or m.group(2) or "", t)
    t = _MD_ITALIC.sub(lambda m: m.group(1) or m.group(2) or "", t)
    t = _EMOJI_HEAVY.sub("", t)
    lines = []
    for line in t.splitlines():
        s = line.strip()
        if not s:
            continue
        if s in ("✅", "❌") or s.startswith("✅") or s.startswith("❌"):
            continue
        if s.startswith("($") and s.endswith(")"):
            continue
        lines.append(s)
    t = " ".join(lines)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ""
    if max_chars <= 0 or len(t) <= max_chars:
        return t
    # Prefer ending on a sentence boundary within the budget.
    budget = t[:max_chars]
    parts = _SENTENCE_END.split(budget)
    if len(parts) >= 2:
        # Re-join complete sentences that fit (drop the trailing partial).
        built = ""
        for sent in parts[:-1]:
            candidate = (built + " " + sent).strip() if built else sent.strip()
            if len(candidate) <= max_chars:
                built = candidate
            else:
                break
        if built:
            return built
    # Fall back to last word boundary.
    cut = budget.rsplit(" ", 1)[0].rstrip(" ,;:")
    return (cut or budget).rstrip() + "…"


def load_xai_token(
    auth_path: Path | str | None = None,
    env_api_key: Optional[str] = None,
) -> str:
    """
    Prefer explicit XAI_API_KEY / env, else OIDC token from ~/.grok/auth.json
    (same SuperGrok / X Premium+ login used by the Grok Build CLI).
    """
    if env_api_key:
        return env_api_key.strip()
    env = os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY")
    if env:
        return env.strip()
    path = Path(auth_path) if auth_path else DEFAULT_AUTH_PATH
    if not path.is_file():
        raise SpeechError(
            f"no xAI credentials: set XAI_API_KEY or run `grok login` (missing {path})"
        )
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        raise SpeechError(f"cannot read {path}: {e}") from e
    if not isinstance(data, dict) or not data:
        raise SpeechError(f"empty auth file: {path}")
    for _issuer, entry in data.items():
        if not isinstance(entry, dict):
            continue
        token = entry.get("key") or entry.get("access_token")
        if token:
            return str(token)
    raise SpeechError(f"no access token in {path} — run `grok login --oauth`")


async def _run(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise SpeechError(f"timed out: {' '.join(cmd[:3])}…")
    out = (out_b or b"").decode("utf-8", "replace")
    err = (err_b or b"").decode("utf-8", "replace")
    return proc.returncode or 0, out, err


class SpeechService:
    """
    STT/TTS with pluggable backends:

    - provider=grok  — xAI Grok Voice APIs using SuperGrok OAuth (or XAI_API_KEY)
    - provider=local — Whisper CLI + edge-tts (offline-ish fallback)

    Performance notes (Grok):
    - TTS time scales with character count; keep speech_max_chars low for snappy replies.
    - Reuses one aiohttp session + cached OAuth token across calls.
    """

    def __init__(
        self,
        provider: str = "grok",
        # Grok / xAI
        xai_api_key: Optional[str] = None,
        auth_path: Optional[Path] = None,
        grok_tts_voice: str = "ara",
        grok_language: str = "en",
        speech_max_chars: int = DEFAULT_SPEECH_MAX_CHARS,
        # Local fallback
        whisper_bin: str = "whisper",
        edge_tts_bin: str = "edge-tts",
        ffmpeg_bin: str = "ffmpeg",
        whisper_model: str = "base",
        edge_tts_voice: str = "en-GB-RyanNeural",
        work_dir: Optional[Path] = None,
        stt_timeout_s: float = 180.0,
        tts_timeout_s: float = 60.0,
    ):
        self.provider = (provider or "grok").strip().lower()
        if self.provider not in ("grok", "local"):
            raise SpeechError(f"unknown speech provider: {provider}")
        self.xai_api_key = xai_api_key
        self.auth_path = Path(auth_path) if auth_path else DEFAULT_AUTH_PATH
        self.grok_tts_voice = grok_tts_voice
        self.grok_language = grok_language
        self.speech_max_chars = speech_max_chars
        self.whisper_bin = whisper_bin
        self.edge_tts_bin = edge_tts_bin
        self.ffmpeg_bin = ffmpeg_bin
        self.whisper_model = whisper_model
        self.edge_tts_voice = edge_tts_voice
        self.work_dir = Path(work_dir) if work_dir else Path(tempfile.gettempdir()) / "grok-telegram-speech"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.stt_timeout_s = stt_timeout_s
        self.tts_timeout_s = tts_timeout_s
        self._session: aiohttp.ClientSession | None = None
        self._token_cache: str | None = None
        self._token_mtime: float | None = None

    def available(self) -> dict[str, bool]:
        if self.provider == "grok":
            try:
                self._token()
                ok = True
            except SpeechError:
                ok = False
            ff = shutil.which(self.ffmpeg_bin) is not None
            return {"stt": ok, "tts": ok and ff, "provider": True}
        return {
            "stt": shutil.which(self.whisper_bin) is not None,
            "tts": shutil.which(self.edge_tts_bin) is not None
            and shutil.which(self.ffmpeg_bin) is not None,
            "provider": True,
        }

    def _token(self) -> str:
        if self.xai_api_key:
            return self.xai_api_key.strip()
        env = os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY")
        if env:
            return env.strip()
        try:
            mtime = self.auth_path.stat().st_mtime
        except OSError:
            mtime = None
        if self._token_cache and self._token_mtime == mtime:
            return self._token_cache
        token = load_xai_token(self.auth_path, None)
        self._token_cache = token
        self._token_mtime = mtime
        return token

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=max(self.stt_timeout_s, self.tts_timeout_s))
            # Reuse TCP connections to api.x.ai across STT/TTS calls in a turn.
            connector = aiohttp.TCPConnector(limit=4, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def transcribe(self, audio_path: str | Path, language: str = "en") -> str:
        audio_path = Path(audio_path)
        if not audio_path.is_file():
            raise SpeechError(f"audio not found: {audio_path}")
        if self.provider == "grok":
            return await self._transcribe_grok(audio_path, language=language)
        return await self._transcribe_local(audio_path, language=language)

    async def synthesize(
        self,
        text: str,
        voice: Optional[str] = None,
        max_chars: Optional[int] = None,
    ) -> Path:
        """Return path to an OGG/Opus voice note suitable for Telegram send_voice."""
        limit = self.speech_max_chars if max_chars is None else max_chars
        clean = text_for_speech(text, max_chars=limit)
        if not clean:
            raise SpeechError("nothing to speak")
        if self.provider == "grok":
            return await self._synthesize_grok(clean, voice=voice)
        return await self._synthesize_local(clean, voice=voice)

    # --- Grok / xAI -------------------------------------------------------

    async def _transcribe_grok(self, audio_path: Path, language: str) -> str:
        token = self._token()
        form = aiohttp.FormData()
        form.add_field("format", "true")
        form.add_field("language", language or self.grok_language)
        mime = "audio/ogg"
        suffix = audio_path.suffix.lower()
        if suffix == ".mp3":
            mime = "audio/mpeg"
        elif suffix == ".wav":
            mime = "audio/wav"
        elif suffix in (".m4a", ".mp4"):
            mime = "audio/mp4"
        form.add_field(
            "file",
            audio_path.read_bytes(),
            filename=audio_path.name or "audio.ogg",
            content_type=mime,
        )
        headers = {"Authorization": f"Bearer {token}"}
        try:
            session = await self._http()
            async with session.post(XAI_STT_URL, data=form, headers=headers) as resp:
                body = await resp.text()
                if resp.status != 200:
                    raise SpeechError(f"Grok STT HTTP {resp.status}: {body[:400]}")
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as e:
                    raise SpeechError(f"Grok STT bad JSON: {e}") from e
        except SpeechError:
            raise
        except Exception as e:
            raise SpeechError(f"Grok STT request failed: {type(e).__name__}: {e}") from e
        text = (payload.get("text") or "").strip()
        if not text:
            raise SpeechError("Grok STT returned empty transcript")
        return text

    async def _synthesize_grok(self, text: str, voice: Optional[str] = None) -> Path:
        token = self._token()
        if shutil.which(self.ffmpeg_bin) is None:
            raise SpeechError(f"ffmpeg not found: {self.ffmpeg_bin}")
        voice_id = voice or self.grok_tts_voice
        # Leaner MP3: smaller download, slightly faster synthesis.
        payload = {
            "text": text,
            "voice_id": voice_id,
            "language": self.grok_language,
            "output_format": {
                "codec": "mp3",
                "sample_rate": 24000,
                "bit_rate": 64000,
            },
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            session = await self._http()
            async with session.post(XAI_TTS_URL, json=payload, headers=headers) as resp:
                data = await resp.read()
                if resp.status != 200:
                    raise SpeechError(
                        f"Grok TTS HTTP {resp.status}: {data[:400].decode('utf-8', 'replace')}"
                    )
                if not data:
                    raise SpeechError("Grok TTS returned empty audio")
        except SpeechError:
            raise
        except Exception as e:
            raise SpeechError(f"Grok TTS request failed: {type(e).__name__}: {e}") from e

        work = Path(tempfile.mkdtemp(prefix="tts-grok-", dir=self.work_dir))
        mp3 = work / "speech.mp3"
        ogg = work / "speech.ogg"
        try:
            mp3.write_bytes(data)
            return await self._mp3_to_ogg(mp3, ogg)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def _mp3_to_ogg(self, mp3: Path, ogg: Path) -> Path:
        # Fast Opus for voice notes (voip + low compression level).
        ff = [
            self.ffmpeg_bin, "-y",
            "-i", str(mp3),
            "-c:a", "libopus",
            "-b:a", "24k",
            "-application", "voip",
            "-compression_level", "0",
            str(ogg),
        ]
        rc, out, err = await _run(ff, 30.0)
        if rc != 0 or not ogg.is_file() or ogg.stat().st_size == 0:
            raise SpeechError(f"ffmpeg failed (rc={rc}): {(err or out).strip()[:500]}")
        final = self.work_dir / f"voice-{time.time_ns()}.ogg"
        shutil.move(str(ogg), str(final))
        return final

    # --- Local fallback ---------------------------------------------------

    async def _transcribe_local(self, audio_path: Path, language: str) -> str:
        if shutil.which(self.whisper_bin) is None:
            raise SpeechError(f"whisper not found: {self.whisper_bin}")
        out_dir = Path(tempfile.mkdtemp(prefix="stt-", dir=self.work_dir))
        try:
            cmd = [
                self.whisper_bin,
                str(audio_path),
                "--model", self.whisper_model,
                "--language", language,
                "--output_dir", str(out_dir),
                "--output_format", "txt",
                "--verbose", "False",
                "--fp16", "False",
            ]
            rc, out, err = await _run(cmd, self.stt_timeout_s)
            if rc != 0:
                raise SpeechError(f"whisper failed (rc={rc}): {(err or out).strip()[:500]}")
            candidates = list(out_dir.glob("*.txt"))
            if not candidates:
                raise SpeechError("whisper produced no transcript")
            text = candidates[0].read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                raise SpeechError("empty transcript")
            return text
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    async def _synthesize_local(self, text: str, voice: Optional[str] = None) -> Path:
        if shutil.which(self.edge_tts_bin) is None:
            raise SpeechError(f"edge-tts not found: {self.edge_tts_bin}")
        if shutil.which(self.ffmpeg_bin) is None:
            raise SpeechError(f"ffmpeg not found: {self.ffmpeg_bin}")
        voice = voice or self.edge_tts_voice
        work = Path(tempfile.mkdtemp(prefix="tts-local-", dir=self.work_dir))
        mp3 = work / "speech.mp3"
        ogg = work / "speech.ogg"
        try:
            cmd = [
                self.edge_tts_bin,
                "--text", text,
                "--voice", voice,
                "--write-media", str(mp3),
            ]
            rc, out, err = await _run(cmd, self.tts_timeout_s)
            if rc != 0 or not mp3.is_file() or mp3.stat().st_size == 0:
                raise SpeechError(f"edge-tts failed (rc={rc}): {(err or out).strip()[:500]}")
            return await self._mp3_to_ogg(mp3, ogg)
        finally:
            shutil.rmtree(work, ignore_errors=True)
