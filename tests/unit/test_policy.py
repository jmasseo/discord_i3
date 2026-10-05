from __future__ import annotations

from i3bridge.bridge.policy import FloodBreaker, RateLimiter, source_matches


def test_source_matches() -> None:
    pats = ["spammer@badmud", "@evilmud", "wholemud", "troll*@*"]
    assert source_matches(pats, "BadMud", "Spammer")
    assert not source_matches(pats, "BadMud", "nice")
    assert source_matches(pats, "EvilMud", "anyone")
    assert source_matches(pats, "WholeMud", None)
    assert source_matches(pats, "Else", "troll42")
    assert not source_matches([], "x", "y")


def test_rate_limiter() -> None:
    t = [0.0]
    rl = RateLimiter(2, 10.0, clock=lambda: t[0])
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a")
    assert rl.allow("b")
    t[0] = 11.0
    assert rl.allow("a")
    rl.forget("a")
    assert rl.allow("a") and rl.allow("a")
    assert RateLimiter(0, 1.0).allow("x")


def test_flood_breaker() -> None:
    t = [0.0]
    fb = FloodBreaker(3, 10.0, clock=lambda: t[0])
    assert [fb.record("c") for _ in range(3)] == ["pass", "pass", "pass"]
    assert fb.record("c") == "trip"
    assert fb.record("c") == "drop"
    assert fb.muted("c")
    assert fb.take_dropped("c") == 2 and fb.take_dropped("c") == 0
    t[0] = 11.0
    assert fb.record("c") == "pass"
    assert not fb.muted("c")
    assert FloodBreaker(0, 1.0).record("c") == "pass"
