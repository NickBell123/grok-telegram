import asyncio
import logging
from pathlib import Path

from aiohttp import web

from grok_telegram.bot import Bot
from grok_telegram.config import Config
from grok_telegram.log import JsonlLogger
from grok_telegram.push import build_app
from grok_telegram.ratelimit import SlidingWindowLimiter
from grok_telegram.runner import GrokRunner
from grok_telegram.speech import SpeechService
from grok_telegram.state import StateStore


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs every request URL at INFO, and the Telegram API carries the bot
    # token in the path — that would write the token to the journal in plaintext.
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _main() -> None:
    configure_logging()
    cfg = Config.from_env()
    state_dir = Path(cfg.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tmp").mkdir(parents=True, exist_ok=True)
    state = StateStore(state_dir / "state.json")
    logger = JsonlLogger(state_dir / "log.jsonl")
    limiter = SlidingWindowLimiter(max_events=cfg.rate_limit_per_hour, window_seconds=3600)
    speech = SpeechService(
        provider=cfg.speech_provider,
        xai_api_key=cfg.xai_api_key,
        auth_path=Path(cfg.grok_auth_path),
        grok_tts_voice=cfg.grok_tts_voice,
        grok_language=cfg.grok_speech_language,
        speech_max_chars=cfg.speech_max_chars,
        whisper_bin=cfg.whisper_bin,
        edge_tts_bin=cfg.edge_tts_bin,
        ffmpeg_bin=cfg.ffmpeg_bin,
        whisper_model=cfg.whisper_model,
        edge_tts_voice=cfg.edge_tts_voice,
        work_dir=state_dir / "tmp" / "speech",
    )
    avail = speech.available()
    voice = cfg.grok_tts_voice if cfg.speech_provider == "grok" else cfg.edge_tts_voice
    logging.info(
        "speech provider=%s stt=%s tts=%s voice=%s model=%s",
        cfg.speech_provider,
        avail.get("stt"),
        avail.get("tts"),
        voice,
        cfg.grok_model,
    )
    bot = Bot(
        config=cfg,
        state=state,
        runner_factory=lambda: GrokRunner(grok_cmd=[cfg.grok_bin], model=cfg.grok_model),
        logger=logger,
        limiter=limiter,
        speech=speech,
    )

    async def push_send(chat_id: str, text: str) -> int:
        return await bot.send(chat_id, text)

    push_app = build_app(
        push_token=cfg.push_token,
        default_chat_id=cfg.default_chat_id,
        send=push_send,
    )

    # Start Telegram polling and push server concurrently.
    await bot.app.initialize()
    await bot.app.start()
    polling = asyncio.create_task(bot.app.updater.start_polling())

    runner_app = web.AppRunner(push_app)
    await runner_app.setup()
    site = web.TCPSite(runner_app, cfg.push_host, cfg.push_port)
    await site.start()
    logging.info("listening on %s:%d", cfg.push_host, cfg.push_port)

    stop = asyncio.Event()
    try:
        await stop.wait()
    finally:
        polling.cancel()
        await bot.app.updater.stop()
        await bot.app.stop()
        await bot.app.shutdown()
        await runner_app.cleanup()


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
