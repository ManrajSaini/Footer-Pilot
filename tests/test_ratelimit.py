from app.web.ratelimit import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_the_limit_then_blocks():
    clock = FakeClock()
    limiter = RateLimiter(limit=3, window_seconds=60, clock=clock)
    assert [limiter.retry_after("a") for _ in range(3)] == [None, None, None]
    clock.now += 20
    assert limiter.retry_after("a") == 40


def test_window_resets():
    clock = FakeClock()
    limiter = RateLimiter(limit=1, window_seconds=60, clock=clock)
    assert limiter.retry_after("a") is None
    assert limiter.retry_after("a") is not None
    clock.now += 60
    assert limiter.retry_after("a") is None


def test_clients_are_counted_separately():
    limiter = RateLimiter(limit=1, window_seconds=60, clock=FakeClock())
    assert limiter.retry_after("a") is None
    assert limiter.retry_after("b") is None
    assert limiter.retry_after("a") is not None
    assert limiter.retry_after("b") is not None


def test_blocked_requests_do_not_extend_the_block():
    clock = FakeClock()
    limiter = RateLimiter(limit=1, window_seconds=60, clock=clock)
    limiter.retry_after("a")
    for _ in range(10):
        clock.now += 5
        limiter.retry_after("a")
    clock.now += 10  # 60 seconds after the first hit
    assert limiter.retry_after("a") is None
