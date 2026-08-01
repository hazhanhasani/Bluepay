import pytest

from app.core.rate_limit import FixedWindowRateLimiter


@pytest.mark.asyncio
async def test_fixed_window_blocks_after_limit():
    limiter = FixedWindowRateLimiter()
    first = await limiter._consume("api_key", "hash", 2)
    second = await limiter._consume("api_key", "hash", 2)
    third = await limiter._consume("api_key", "hash", 2)
    assert first.allowed is True
    assert second.allowed is True
    assert third.allowed is False
    assert third.remaining == 0
    assert third.retry_after >= 1


@pytest.mark.asyncio
async def test_scopes_have_independent_counters():
    limiter = FixedWindowRateLimiter()
    key_decision = await limiter._consume("api_key", "same", 1)
    ip_decision = await limiter._consume("ip", "same", 1)
    assert key_decision.allowed is True
    assert ip_decision.allowed is True
