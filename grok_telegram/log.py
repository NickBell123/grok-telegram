import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class JsonlLogger:
    def __init__(self, path: Path, now_fn: Callable[[], str] = _now_iso):
        self.path = Path(path)
        self.now_fn = now_fn

    def write(self, kind: str, chat_id: Optional[str] = None, details: Optional[dict[str, Any]] = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": self.now_fn(),
            "kind": kind,
            "chat_id": chat_id,
            "details": details or {},
        }
        with self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
