import json
import os
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

# off = never speak replies
# on  = always speak text replies
# auto = speak when the user sent a voice note
TTS_MODES = frozenset({"off", "on", "auto"})

# grok = xAI Voice APIs (subscription OAuth)
# local = Whisper + edge-tts
SPEECH_PROVIDERS = frozenset({"grok", "local"})


@dataclass(frozen=True)
class ChatState:
    session_id: Optional[str]
    cwd: str
    tts_mode: str = "auto"
    # None means "use Config.speech_provider default"
    speech_provider: Optional[str] = None
    # None means "use Config.grok_model default"
    grok_model: Optional[str] = None


class StateStore:
    def __init__(self, path: Path, default_cwd: str | None = None):
        default_cwd = default_cwd or os.path.expanduser("~")
        self.path = Path(path)
        self.default_cwd = default_cwd
        self._data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"chats": {}}
        with self.path.open() as f:
            return json.load(f)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state.", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            os.unlink(tmp)
            raise

    def _normalize_provider(self, raw) -> Optional[str]:
        if raw is None or raw == "":
            return None
        p = str(raw).strip().lower()
        if p not in SPEECH_PROVIDERS:
            return None
        return p

    def _normalize_model(self, raw) -> Optional[str]:
        if raw is None:
            return None
        m = str(raw).strip()
        return m or None

    def get(self, chat_id: str) -> ChatState:
        d = self._data["chats"].get(chat_id)
        if d is None:
            return ChatState(
                session_id=None,
                cwd=self.default_cwd,
                tts_mode="auto",
                speech_provider=None,
                grok_model=None,
            )
        mode = d.get("tts_mode", "auto")
        if mode not in TTS_MODES:
            mode = "auto"
        return ChatState(
            session_id=d.get("session_id"),
            cwd=d.get("cwd", self.default_cwd),
            tts_mode=mode,
            speech_provider=self._normalize_provider(d.get("speech_provider")),
            grok_model=self._normalize_model(d.get("grok_model")),
        )

    def set(self, chat_id: str, state: ChatState) -> None:
        self._data["chats"][chat_id] = asdict(state)
        self._save()

    def _update(self, chat_id: str, **kwargs) -> ChatState:
        current = self.get(chat_id)
        new = ChatState(
            session_id=kwargs["session_id"] if "session_id" in kwargs else current.session_id,
            cwd=kwargs["cwd"] if "cwd" in kwargs else current.cwd,
            tts_mode=kwargs["tts_mode"] if "tts_mode" in kwargs else current.tts_mode,
            speech_provider=kwargs["speech_provider"] if "speech_provider" in kwargs else current.speech_provider,
            grok_model=kwargs["grok_model"] if "grok_model" in kwargs else current.grok_model,
        )
        self.set(chat_id, new)
        return new

    def set_session(self, chat_id: str, session_id: str) -> None:
        self._update(chat_id, session_id=session_id)

    def set_cwd(self, chat_id: str, cwd: str) -> None:
        self._update(chat_id, cwd=cwd)

    def set_tts_mode(self, chat_id: str, mode: str) -> None:
        if mode not in TTS_MODES:
            raise ValueError(f"tts_mode must be one of {sorted(TTS_MODES)}")
        self._update(chat_id, tts_mode=mode)

    def set_speech_provider(self, chat_id: str, provider: Optional[str]) -> None:
        """Set per-chat provider, or None to fall back to config default."""
        if provider is not None and provider not in SPEECH_PROVIDERS:
            raise ValueError(f"speech_provider must be one of {sorted(SPEECH_PROVIDERS)}")
        self._update(chat_id, speech_provider=provider)

    def set_grok_model(self, chat_id: str, model: Optional[str]) -> None:
        """Set per-chat model, or None to fall back to Config.grok_model."""
        if model is not None:
            model = model.strip()
            if not model:
                raise ValueError("grok_model must be a non-empty model id")
        self._update(chat_id, grok_model=model)

    def clear_session(self, chat_id: str) -> None:
        self._update(chat_id, session_id=None)
