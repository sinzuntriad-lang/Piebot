"""Send-rate protection so the account doesn't spam Messenger.

  - one reply at a time per GC (and extra messages are skipped when a GC already has a backlog)
  - at most N sends in flight across all GCs
  - minimum gap between two sends in the same GC
  - retry with backoff + cooldown when Messenger says "temporarily unavailable" (error 1545012)
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Awaitable, Callable, TypeVar

import config

log = logging.getLogger("traffic")
T = TypeVar("T")

_THROTTLE_RE = re.compile(r"1545012|temporarily unavailable|message could not be sent|rate.?limit|too many", re.I)


class ThrottledError(RuntimeError):
    pass


def is_throttle_error(exc: BaseException | None) -> bool:
    return bool(exc) and bool(_THROTTLE_RE.search(f"{type(exc).__name__} {exc}"))


class TrafficGate:
    RETRY_DELAYS = (1.5, 4.0, 8.0)

    def __init__(self, global_limit: int | None = None, thread_cooldown_s: float | None = None,
                 max_pending: int | None = None):
        self._sem = asyncio.Semaphore(global_limit or config.GLOBAL_SEND_LIMIT)
        self.thread_gap_s = config.THREAD_COOLDOWN_S if thread_cooldown_s is None else thread_cooldown_s
        self.max_pending = max_pending or config.MAX_PENDING_PER_THREAD
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending: dict[str, int] = {}
        self._last_sent: dict[str, float] = {}
        self._cooldown_until: dict[str, float] = {}

    # ---- per-thread slot ------------------------------------------------
    def try_reserve(self, thread_id) -> bool:
        key = str(thread_id)
        if self._pending.get(key, 0) >= self.max_pending:
            return False
        self._pending[key] = self._pending.get(key, 0) + 1
        return True

    def release(self, thread_id) -> None:
        key = str(thread_id)
        left = self._pending.get(key, 0) - 1
        if left > 0:
            self._pending[key] = left
        else:
            self._pending.pop(key, None)
            lock = self._locks.get(key)
            if lock and not lock.locked():
                self._locks.pop(key, None)

    def lock(self, thread_id) -> asyncio.Lock:
        return self._locks.setdefault(str(thread_id), asyncio.Lock())

    def cooling_down(self) -> int:
        now = time.monotonic()
        return sum(1 for t in self._cooldown_until.values() if t > now)

    # ---- the actual send --------------------------------------------------
    async def send(self, thread_id, do_send: Callable[[], Awaitable[T]]) -> T:
        key = str(thread_id)
        now = time.monotonic()
        until = self._cooldown_until.get(key, 0)
        if until > now:
            raise ThrottledError(f"thread cooldown active for {until - now:.0f}s")

        gap = self.thread_gap_s - (now - self._last_sent.get(key, 0))
        if gap > 0:
            await asyncio.sleep(gap)

        async with self._sem:
            last: BaseException | None = None
            for attempt in range(len(self.RETRY_DELAYS) + 1):
                try:
                    result = await do_send()
                    self._last_sent[key] = time.monotonic()
                    return result
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last = exc
                    if not is_throttle_error(exc) or attempt >= len(self.RETRY_DELAYS):
                        break
                    delay = self.RETRY_DELAYS[attempt]
                    self._cooldown_until[key] = time.monotonic() + min(15, delay)
                    await asyncio.sleep(delay)
                    self._cooldown_until.pop(key, None)
            if is_throttle_error(last):
                self._cooldown_until[key] = time.monotonic() + 5 * 60
                log.warning("thread %s throttled by Messenger, cooling down 5 min", key)
            assert last is not None
            raise last
