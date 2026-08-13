import hmac
from typing import Awaitable, Callable

from aiohttp import web

SendFn = Callable[[str, str], Awaitable[int]]


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
        ids = []
        for chunk in _chunks(text, max_chars):
            mid = await send(str(chat_id), chunk)
            ids.append(mid)
        return web.json_response({"ok": True, "message_ids": ids})

    app = web.Application()
    app.router.add_post("/push", handle_push)
    return app
