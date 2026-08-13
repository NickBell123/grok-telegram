import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    allowed_user_id: int
    default_chat_id: str
    push_token: str
    push_host: str = "127.0.0.1"
    # 8788 so claude-telegram can keep 8787 on the same host.
    push_port: int = 8788
    state_dir: str = os.path.expanduser("~/.grok-telegram")
    rate_limit_per_hour: int = 60
    edit_min_interval_s: float = 1.2
    max_telegram_chars: int = 3900
    grok_bin: str = "grok"
    # Passed as `grok --model`. Resume keeps the session's old model unless set.
    grok_model: str = "grok-4.6"
    # Speech — default provider is Grok Voice APIs (subscription OAuth).
    stt_enabled: bool = True
    tts_enabled: bool = True
    speech_provider: str = "grok"  # grok | local
    xai_api_key: str | None = None
    grok_auth_path: str = os.path.expanduser("~/.grok/auth.json")
    grok_tts_voice: str = "ara"
    grok_speech_language: str = "en"
    # Spoken replies are truncated to this many chars (Grok TTS scales with length).
    speech_max_chars: int = 280
    whisper_bin: str = "whisper"
    whisper_model: str = "base"
    edge_tts_bin: str = "edge-tts"
    ffmpeg_bin: str = "ffmpeg"
    edge_tts_voice: str = "en-GB-RyanNeural"

    @classmethod
    def from_env(cls) -> "Config":
        def req(name: str) -> str:
            v = os.environ.get(name)
            if not v:
                raise ConfigError(f"{name} is required")
            return v

        def flag(name: str, default: bool = True) -> bool:
            v = os.environ.get(name)
            if v is None:
                return default
            return v.strip().lower() in ("1", "true", "yes", "on")

        try:
            allowed = int(req("ALLOWED_USER_ID"))
        except ValueError as e:
            raise ConfigError(f"ALLOWED_USER_ID must be an integer: {e}")

        push_port = int(os.environ.get("PUSH_PORT", "8788"))
        provider = (os.environ.get("SPEECH_PROVIDER") or "grok").strip().lower()
        if provider not in ("grok", "local"):
            raise ConfigError("SPEECH_PROVIDER must be 'grok' or 'local'")

        # TTS_VOICE is the active voice for the selected provider.
        default_voice = "ara" if provider == "grok" else "en-GB-RyanNeural"
        tts_voice = os.environ.get("TTS_VOICE") or os.environ.get("GROK_TTS_VOICE") or default_voice

        return cls(
            telegram_bot_token=req("TELEGRAM_BOT_TOKEN"),
            allowed_user_id=allowed,
            default_chat_id=req("DEFAULT_CHAT_ID"),
            push_token=req("PUSH_TOKEN"),
            push_port=push_port,
            grok_bin=os.environ.get("GROK_BIN", "grok"),
            grok_model=(os.environ.get("GROK_MODEL") or "grok-4.6").strip(),
            stt_enabled=flag("STT_ENABLED", True),
            tts_enabled=flag("TTS_ENABLED", True),
            speech_provider=provider,
            xai_api_key=os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY") or None,
            grok_auth_path=os.environ.get("GROK_AUTH_PATH", str(Path.home() / ".grok" / "auth.json")),
            grok_tts_voice=tts_voice if provider == "grok" else "ara",
            grok_speech_language=os.environ.get("GROK_SPEECH_LANGUAGE", "en"),
            speech_max_chars=int(os.environ.get("SPEECH_MAX_CHARS", "280")),
            whisper_bin=os.environ.get("WHISPER_BIN", "whisper"),
            whisper_model=os.environ.get("WHISPER_MODEL", "base"),
            edge_tts_bin=os.environ.get("EDGE_TTS_BIN", "edge-tts"),
            ffmpeg_bin=os.environ.get("FFMPEG_BIN", "ffmpeg"),
            edge_tts_voice=tts_voice if provider == "local" else "en-GB-RyanNeural",
        )
