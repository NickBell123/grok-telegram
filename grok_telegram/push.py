import hmac
from typing import Awaitable, Callable

from aiohttp import web

from grok_telegram.richtext import markdown_to_telegram_html

# parse_mode is optional so existing plain-text callers keep working.
SendFn = Callable[..., Awaitable[int]]


def _chunks(text: str, n: int) -> list[str]:
    if not text:
        return [""]
    return [text[i : i + n] for i in range(0, len(text), n)]


def build_app(
    push_token: str,
    default_chat_id: str,
    send: SendFn,
    max_chars: int = 4000,
) -> web.Application:
    async def handle_push(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        # Constant-time compare so a caller can't time-probe the token byte by byte.
        if not hmac.compare_digest(auth, f"Bearer {push_token}"):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        text = body.get("text")
        if not isinstance(text, str) or not text:
            return web.json_response({"ok": False, "error": "text required"}, status=400)
        chat_id = body.get("chat_id") or default_chat_id
        # Opt-in markdown: cron callers post GFM, which reached Telegram with
        # literal ** because nothing converted it. Chunk on the source text so
        # a tag can't be split, then convert each chunk independently.
        markdown = bool(body.get("markdown"))
        title = body.get("title")
        if markdown and isinstance(title, str) and title:
            text = f"**{title}**\n\n{text}"
        ids = []
        for chunk in _chunks(text, max_chars):
            if not markdown:
                # Unchanged plain path: no parse_mode, so a fake send that
                # takes only (chat_id, text) keeps working.
                ids.append(await send(str(chat_id), chunk))
                continue
            ids.append(await send(str(chat_id), markdown_to_telegram_html(chunk), "HTML"))
        return web.json_response({"ok": True, "message_ids": ids})

    app = web.Application()
    app.router.add_post("/push", handle_push)
    return app
