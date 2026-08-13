import logging

from grok_telegram.__main__ import configure_logging


def test_httpx_request_logging_is_suppressed():
    """
    httpx logs request URLs at INFO and the Telegram API puts the bot token in
    the path, so INFO-level httpx logging leaks the token into the journal.
    """
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    configure_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
