"""Convert assistant/markdown-ish text to Telegram HTML (ParseMode.HTML)."""

from __future__ import annotations

import html
import re


def escape(text: str) -> str:
    return html.escape(text, quote=False)


# Fenced code blocks ```lang\n...\n```
_FENCE = re.compile(r"```(?:([\w+-]+)\n)?([\s\S]*?)```", re.MULTILINE)
# Inline code `...`
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
# Links [label](url)
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
# Bold **...** or __...__
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
# Italic *...* only (underscore italic disabled — conflicts with identifiers)
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
# Strikethrough ~~...~~
_STRIKE = re.compile(r"~~(.+?)~~")
# Headings at line start
_HEADING = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
# Tool lines produced by StreamRenderer (before HTML escape)
_TOOL_LINE = re.compile(r"^>\s*🔧\s*(.+)$", re.MULTILINE)
# Unordered list items
_UL = re.compile(r"^(\s*)[-*+]\s+(.+)$", re.MULTILINE)


def _protect(segments: list[str], text: str) -> str:
    """Replace text with a placeholder index into segments."""
    idx = len(segments)
    segments.append(text)
    return f"\x00SEG{idx}\x00"


def _restore(segments: list[str], text: str) -> str:
    def repl(m: re.Match) -> str:
        i = int(m.group(1))
        return segments[i]

    return re.sub(r"\x00SEG(\d+)\x00", repl, text)


def markdown_to_telegram_html(text: str) -> str:
    """
    Best-effort Markdown → Telegram HTML.

    Escapes raw HTML, then applies a limited markdown subset that Telegram
    can render: bold, italic (*only*), strike, code, pre, links, headings,
    lists, and our tool-use blockquotes.
    """
    if not text:
        return ""

    segments: list[str] = []

    # 0) Protect tool lines early (before `>` is escaped)
    def tool_repl(m: re.Match) -> str:
        return _protect(segments, f"<blockquote>🔧 {escape(m.group(1))}</blockquote>")

    t = _TOOL_LINE.sub(tool_repl, text)

    # 1) Protect fenced code blocks
    def fence_repl(m: re.Match) -> str:
        body = escape(m.group(2).rstrip("\n"))
        return _protect(segments, f"<pre>{body}</pre>")

    t = _FENCE.sub(fence_repl, t)

    # 2) Protect inline code
    def code_repl(m: re.Match) -> str:
        return _protect(segments, f"<code>{escape(m.group(1))}</code>")

    t = _INLINE_CODE.sub(code_repl, t)

    # 3) Protect links
    def link_repl(m: re.Match) -> str:
        label = escape(m.group(1))
        url = m.group(2)
        if not url.startswith(("http://", "https://")):
            return escape(m.group(0))
        return _protect(segments, f'<a href="{html.escape(url, quote=True)}">{label}</a>')

    t = _LINK.sub(link_repl, t)

    # 4) Escape remaining plain text (leave placeholders intact)
    parts = re.split(r"(\x00SEG\d+\x00)", t)
    for i, part in enumerate(parts):
        if part.startswith("\x00SEG"):
            continue
        parts[i] = escape(part)
    t = "".join(parts)

    # 5) Bold / strike / italic on escaped text
    def bold_repl(m: re.Match) -> str:
        inner = m.group(1) or m.group(2) or ""
        return f"<b>{inner}</b>"

    t = _BOLD.sub(bold_repl, t)

    def strike_repl(m: re.Match) -> str:
        return f"<s>{m.group(1)}</s>"

    t = _STRIKE.sub(strike_repl, t)

    def italic_repl(m: re.Match) -> str:
        return f"<i>{m.group(1)}</i>"

    t = _ITALIC.sub(italic_repl, t)

    # 6) Headings → bold lines
    t = _HEADING.sub(lambda m: f"<b>{m.group(2)}</b>", t)

    # 7) Lists — bullet character
    t = _UL.sub(lambda m: f"{m.group(1)}• {m.group(2)}", t)

    # 8) Status markers at end
    t = t.replace("\n\n✅", "\n\n<b>✅</b>")
    t = t.replace("\n\n❌", "\n\n<b>❌</b>")
    t = re.sub(r"\(\$([0-9.]+)\)", r"<i>($\1)</i>", t)

    return _restore(segments, t)


def safe_html(text: str) -> str:
    """Convert to HTML; if empty, return empty string."""
    return markdown_to_telegram_html(text or "")
