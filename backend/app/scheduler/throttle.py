"""Rate and concurrency limits.

Two independent limits, both taken from what the carrier tells you rather than
guessed:

* **Calls per second** — bursting a 500-row campaign at 50 CPS gets you
  rate-limited or trunk-throttled, and a throttled trunk fails calls that the
  cap has already counted.
* **Concurrent channels** — exceeding the carrier's ceiling earns 503s, and
  repeated 503s earn a conversation with the carrier.

Plus a **per-tenant** concurrency cap, so one company's 5,000-row campaign cannot
consume every channel and starve everyone else on the platform.

Redis-backed, with a documented degradation: when Redis is unavailable the
limiter fails **closed** for origination. A telephony system that keeps dialling
when it has lost track of how fast it is dialling is the one that gets a trunk
suspended.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

# A true token bucket, not a fixed window. Lua so refill, check and consume are
# one atomic step — two workers reading the same count is exactly the race this
# exists to prevent.
#
# The earlier version keyed on `floor(now)`, which is a *fixed window*, and a
# fixed window lets through up to 2x the rate across a boundary: five calls at
# 12:00:00.9 and five more at 12:00:01.0 is ten calls inside one second. Against
# a carrier that caps CPS, that is the burst that gets the trunk throttled — and
# it is invisible in testing because it only happens when a batch straddles a
# second.
#
# Tokens refill continuously at `rate` per second and are capped at `burst`, so
# the limit holds across any window, not just aligned ones.
_TOKEN_BUCKET = """
local key    = KEYS[1]
local rate   = tonumber(ARGV[1])
local burst  = tonumber(ARGV[2])
local now    = tonumber(ARGV[3])

local state  = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts     = tonumber(state[2])

if tokens == nil then
  tokens = burst
  ts = now
end

local elapsed = math.max(0, now - ts)
tokens = math.min(burst, tokens + (elapsed * rate))

local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
-- Long enough that an idle bucket refills to full before it is forgotten.
redis.call('EXPIRE', key, math.ceil(burst / rate) + 60)
return allowed
"""


class Throttle:
    def __init__(
        self,
        redis_client,
        *,
        calls_per_second: int = 5,
        max_concurrent: int = 30,
        max_concurrent_per_tenant: int = 10,
    ):
        self.redis = redis_client
        self.calls_per_second = calls_per_second
        self.max_concurrent = max_concurrent
        self.max_concurrent_per_tenant = max_concurrent_per_tenant
        self._script = None
        if redis_client is not None:
            try:
                self._script = redis_client.register_script(_TOKEN_BUCKET)
            except Exception:  # pragma: no cover - depends on the server
                log.warning("could not register the throttle script")

    # ------------------------------------------------------------------ rate

    def acquire_rate(self) -> bool:
        if self.redis is None or self._script is None:
            return False  # fail closed: see the module docstring
        try:
            return bool(
                self._script(
                    keys=["throttle:cps"],
                    # Burst == rate: no stored allowance beyond one second's
                    # worth, because a carrier's CPS ceiling is a ceiling, not
                    # an average it will forgive you for exceeding.
                    args=[self.calls_per_second, self.calls_per_second, time.time()],
                )
            )
        except Exception:
            log.warning("throttle unavailable; refusing to originate")
            return False

    # ----------------------------------------------------------- concurrency

    def _count(self, key: str) -> int:
        try:
            return int(self.redis.get(key) or 0)
        except Exception:
            return 0

    def acquire_channel(self, company_id) -> bool:
        """Claim one concurrent channel globally and for this tenant."""
        if self.redis is None:
            return False
        global_key = "throttle:channels"
        tenant_key = f"throttle:channels:{company_id}"
        try:
            if self._count(global_key) >= self.max_concurrent:
                return False
            if self._count(tenant_key) >= self.max_concurrent_per_tenant:
                return False
            pipe = self.redis.pipeline()
            pipe.incr(global_key)
            pipe.expire(global_key, 3600)
            pipe.incr(tenant_key)
            pipe.expire(tenant_key, 3600)
            pipe.execute()
            return True
        except Exception:
            log.warning("channel counter unavailable; refusing to originate")
            return False

    def release_channel(self, company_id) -> None:
        if self.redis is None:
            return
        try:
            pipe = self.redis.pipeline()
            # Never below zero: a lost release would otherwise permanently
            # shrink the usable channel pool.
            pipe.eval(
                "if tonumber(redis.call('GET', KEYS[1]) or '0') > 0 then "
                "return redis.call('DECR', KEYS[1]) else return 0 end",
                1,
                "throttle:channels",
            )
            pipe.eval(
                "if tonumber(redis.call('GET', KEYS[1]) or '0') > 0 then "
                "return redis.call('DECR', KEYS[1]) else return 0 end",
                1,
                f"throttle:channels:{company_id}",
            )
            pipe.execute()
        except Exception:
            log.warning("could not release a channel slot for %s", company_id)

    def reset(self) -> None:
        """Test and operator helper. Clears every counter."""
        if self.redis is None:
            return
        for key in self.redis.scan_iter("throttle:*"):
            self.redis.delete(key)
