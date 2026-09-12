import pytest
from aiohttp.test_utils import TestClient, TestServer
from grok_telegram.push import build_app


@pytest.fixture
async def client():
    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id: str, text: str) -> int:
        sent.append((chat_id, text))
        return len(sent)

    app = build_app(push_token="secret", default_chat_id="42", send=fake_send, max_chars=20)
    async with TestClient(TestServer(app)) as c:
        c.sent = sent  # type: ignore[attr-defined]
        yield c


@pytest.mark.asyncio
async def test_rejects_missing_auth(client):
    r = await client.post("/push", json={"text": "hi"})
    assert r.status == 401


@pytest.mark.asyncio
async def test_rejects_wrong_token(client):
    r = await client.post("/push", json={"text": "hi"}, headers={"Authorization": "Bearer wrong"})
    assert r.status == 401


@pytest.mark.asyncio
async def test_accepts_and_sends(client):
    r = await client.post("/push", json={"text": "hi"}, headers={"Authorization": "Bearer secret"})
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    assert len(body["message_ids"]) == 1
    assert client.sent == [("42", "hi")]


@pytest.mark.asyncio
async def test_overrides_chat_id(client):
    r = await client.post(
        "/push",
        json={"text": "hi", "chat_id": "99"},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status == 200
    assert client.sent == [("99", "hi")]


@pytest.mark.asyncio
async def test_chunks_long_text(client):
    long = "A" * 50  # max_chars=20 → 3 chunks
    r = await client.post(
        "/push",
        json={"text": long},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status == 200
    body = await r.json()
    assert len(body["message_ids"]) == 3
    assert "".join(t for _, t in client.sent) == long


# --- markdown rendering on the push path -------------------------------------
# The push path sent raw text with no parse_mode, so GFM from a brief arrived
# with literal ** markers. Opt in with {"markdown": true}, mirroring the chat
# path, which has always rendered via markdown_to_telegram_html.


@pytest.fixture
async def md_client():
    sent: list[tuple[str, str, str | None]] = []

    async def fake_send(chat_id: str, text: str, parse_mode: str | None = None) -> int:
        sent.append((chat_id, text, parse_mode))
        return len(sent)

    app = build_app(push_token="secret", default_chat_id="42", send=fake_send, max_chars=4000)
    async with TestClient(TestServer(app)) as c:
        c.sent = sent  # type: ignore[attr-defined]
        yield c


@pytest.mark.asyncio
async def test_markdown_flag_renders_bold_as_html(md_client):
    r = await md_client.post(
        "/push",
        json={"text": "**📋 Setups scorecard**\n- Win-rate **31%** (83W / 187L)", "markdown": True},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status == 200
    chat_id, text, parse_mode = md_client.sent[0]
    assert parse_mode == "HTML"
    assert "<b>📋 Setups scorecard</b>" in text
    assert "<b>31%</b>" in text
    assert "**" not in text


@pytest.mark.asyncio
async def test_plain_push_is_unchanged_and_has_no_parse_mode(md_client):
    r = await md_client.post(
        "/push",
        json={"text": "**not converted**"},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status == 200
    _, text, parse_mode = md_client.sent[0]
    assert parse_mode is None
    assert text == "**not converted**"


@pytest.mark.asyncio
async def test_title_is_bolded_by_the_server(md_client):
    r = await md_client.post(
        "/push",
        json={"text": "body text", "title": "Daily brief", "markdown": True},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status == 200
    _, text, parse_mode = md_client.sent[0]
    assert parse_mode == "HTML"
    assert text.startswith("<b>Daily brief</b>")
    assert "*" not in text
