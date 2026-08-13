"""Detect local files/images worth sending over Telegram."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Iterable, Optional

# Telegram bot practical limits (Bot API).
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024

IMAGE_EXTS = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff",
})

# Paths we refuse to attach even if they exist (secrets / noise).
DENY_NAME_FRAGMENTS = (
    ".env",
    "id_rsa",
    "id_ed25519",
    "credentials",
    "secret",
    "auth.json",
    ".ssh/",
    "token",
)

# Auto-attach noise: tool/session logs and similar, never photos of these.
NOISE_PATH_FRAGMENTS = (
    "/terminal/",
    "\\terminal\\",
    "/__pycache__/",
    "/.git/",
)

NOISE_EXTS = frozenset({
    ".log", ".jsonl", ".pyc", ".pyo", ".so", ".o", ".a",
    ".tmp", ".swp", ".lock",
})

# Absolute paths and common relative session-style paths ending in a file ext.
_PATH_RE = re.compile(
    r"(?P<path>"
    r"(?:/~|~/|/)[\w./@+%,-]+\.\w{1,8}"  # absolute or ~/
    r"|(?:\.?/)?(?:[\w.-]+/)+[\w.-]+\.\w{1,8}"  # relative with at least one /
    r")"
)


def is_image_path(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTS


def looks_denied(path: str) -> bool:
    lower = path.lower()
    return any(frag in lower for frag in DENY_NAME_FRAGMENTS)


def looks_noise(path: str | Path) -> bool:
    """Session terminal logs and other junk we never auto-attach."""
    s = str(path).replace("\\", "/").lower()
    if any(frag in s for frag in NOISE_PATH_FRAGMENTS):
        return True
    if Path(path).suffix.lower() in NOISE_EXTS:
        return True
    return False


def resolve_existing(path: str, cwd: str) -> Optional[Path]:
    """Return an absolute existing file path, or None."""
    raw = path.strip().strip("`\"'")
    if not raw or "\n" in raw or "\r" in raw:
        return None
    if looks_denied(raw):
        return None
    expanded = os.path.expanduser(raw)
    if os.path.isabs(expanded):
        candidate = Path(expanded)
    else:
        candidate = Path(cwd) / expanded
    try:
        candidate = candidate.resolve()
    except OSError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def extract_path_strings(text: str) -> list[str]:
    """Pull path-like tokens out of free text / tool output."""
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for m in _PATH_RE.finditer(text):
        p = m.group("path")
        # Trim trailing punctuation common in prose.
        p = p.rstrip(".,;:)]}>'\"")
        if p not in seen:
            seen.add(p)
            found.append(p)
    return found


def paths_from_tool_result(content: Any) -> list[str]:
    """Normalize tool_result content (str | list | dict) into path candidates."""
    chunks: list[str] = []
    if content is None:
        return []
    if isinstance(content, str):
        chunks.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                if "text" in item:
                    chunks.append(str(item["text"]))
                elif "content" in item:
                    chunks.append(str(item["content"]))
                else:
                    chunks.append(str(item))
            else:
                chunks.append(str(item))
    elif isinstance(content, dict):
        for key in ("text", "path", "file_path", "filename", "url"):
            if key in content and content[key]:
                chunks.append(str(content[key]))
        chunks.append(str(content))
    else:
        chunks.append(str(content))
    out: list[str] = []
    for c in chunks:
        out.extend(extract_path_strings(c))
        # Bare absolute path with no regex-friendly chars around it.
        stripped = c.strip().strip("`\"'")
        if stripped.startswith("/") or stripped.startswith("~/"):
            out.append(stripped.split()[0] if stripped.split() else stripped)
    return out


def paths_from_tool_use(name: str, inp: dict) -> list[str]:
    """Known tools that take an output/input path."""
    if not isinstance(inp, dict):
        return []
    keys: tuple[str, ...]
    if name in ("write", "search_replace", "read_file"):
        keys = ("file_path", "path", "target_file")
    elif name in ("image_gen", "image_edit", "image_to_video", "reference_to_video"):
        # Results usually carry the path; inputs may include reference images.
        keys = ("image", "path", "file_path", "output")
    else:
        keys = ("file_path", "path", "target_file", "image")
    found: list[str] = []
    for k in keys:
        v = inp.get(k)
        if isinstance(v, str) and v:
            found.append(v)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    found.append(item)
    return found


def file_fingerprint(path: Path) -> str:
    """Content hash so identical copies (session + Desktop) attach once."""
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            while True:
                chunk = f.read(256 * 1024)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        # Fall back to path+size so we still dedupe by path via _seen_paths.
        try:
            st = path.stat()
            return f"stat:{st.st_size}:{st.st_mtime_ns}"
        except OSError:
            return f"path:{path}"
    return h.hexdigest()


class MediaCandidate:
    __slots__ = ("path", "kind")

    def __init__(self, path: Path, kind: str):
        self.path = path
        self.kind = kind  # "photo" | "document"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, MediaCandidate) and self.path == other.path and self.kind == other.kind

    def __hash__(self) -> int:
        return hash((self.path, self.kind))


def classify(path: Path, force: Optional[str] = None) -> Optional[MediaCandidate]:
    """
    Decide photo vs document. force is "photo" | "document" | None.
    Returns None if file is missing, denied, or over size limit.
    """
    if not path.is_file() or looks_denied(str(path)):
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size <= 0:
        return None

    kind = force
    if kind is None:
        kind = "photo" if is_image_path(path) else "document"

    if kind == "photo":
        if size > MAX_PHOTO_BYTES:
            # Fall back to document for large images.
            if size > MAX_DOCUMENT_BYTES:
                return None
            kind = "document"
        elif not is_image_path(path):
            kind = "document"
    if kind == "document" and size > MAX_DOCUMENT_BYTES:
        return None
    return MediaCandidate(path, kind)


class MediaCollector:
    """
    Accumulates sendable local files discovered during a turn.

    Auto-attach defaults to images only and skips terminal logs / noise.
    Use /file for non-image documents. Content-identical copies are deduped.
    """

    def __init__(self, cwd: str, images_only: bool = True):
        self.cwd = cwd
        self.images_only = images_only
        self._ordered: list[MediaCandidate] = []
        self._seen_paths: set[Path] = set()
        self._seen_fingerprints: set[str] = set()

    def add_path(self, raw: str, force: Optional[str] = None) -> None:
        resolved = resolve_existing(raw, self.cwd)
        if resolved is None or resolved in self._seen_paths:
            return
        if looks_noise(resolved):
            return
        if self.images_only and not is_image_path(resolved):
            return
        cand = classify(resolved, force=force)
        if cand is None:
            return
        fp = file_fingerprint(resolved)
        if fp in self._seen_fingerprints:
            return
        self._seen_paths.add(resolved)
        self._seen_fingerprints.add(fp)
        self._ordered.append(cand)

    def note_tool_use(self, name: str, inp: dict) -> None:
        for p in paths_from_tool_use(name, inp or {}):
            # write creates the file; only attach images from write automatically.
            # Non-image writes are often source code — user can /file if wanted.
            if name == "write" and not is_image_path(p):
                continue
            if name == "read_file":
                continue
            if name == "search_replace":
                continue
            force = "photo" if name.startswith("image_") and is_image_path(p) else None
            self.add_path(p, force=force)

    def note_tool_result(self, content: Any) -> None:
        for p in paths_from_tool_result(content):
            self.add_path(p)

    def note_text(self, text: str) -> None:
        for p in extract_path_strings(text):
            self.add_path(p)

    def candidates(self) -> list[MediaCandidate]:
        return list(self._ordered)

    def extend(self, paths: Iterable[str], force: Optional[str] = None) -> None:
        for p in paths:
            self.add_path(p, force=force)
