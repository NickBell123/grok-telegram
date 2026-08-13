from grok_telegram.ratelimit import SlidingWindowLimiter


def test_allows_up_to_limit():
    lim = SlidingWindowLimiter(max_events=3, window_seconds=60.0, now_fn=lambda: 1000.0)
    assert lim.allow("a") is True
    assert lim.allow("a") is True
    assert lim.allow("a") is True
    assert lim.allow("a") is False


def test_separate_keys_have_separate_quotas():
    lim = SlidingWindowLimiter(max_events=1, window_seconds=60.0, now_fn=lambda: 1.0)
    assert lim.allow("a") is True
    assert lim.allow("b") is True
    assert lim.allow("a") is False


def test_events_expire():
    t = [0.0]
    lim = SlidingWindowLimiter(max_events=2, window_seconds=10.0, now_fn=lambda: t[0])
    assert lim.allow("a") is True
    t[0] = 5.0
    assert lim.allow("a") is True
    assert lim.allow("a") is False
    t[0] = 11.0  # first event now outside window
    assert lim.allow("a") is True
