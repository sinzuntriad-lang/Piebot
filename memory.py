"""No-repeat shuffle bag + per-thread reply memory.

ShuffleBag   : every item of a pool is used exactly once per rotation (random order),
               and the first item of a new rotation never equals the last one of the old one.
ReplyMemory  : remembers what was recently said in each thread/GC and keeps those lines
               (and near-duplicates with the same opening words) out of the next picks.
Both are keyed per thread, so every GC has its own independent rotation.
"""
from __future__ import annotations

import random
import re
import time
import unicodedata
from typing import Iterable, Sequence

import config


def norm(value) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = re.sub(r"[‘’“”]", "'", text)
    return re.sub(r"\s+", " ", text).strip()


def signature(value, words: int = 7) -> str:
    """First few words of a line - catches 'same opening, slightly different ending'."""
    text = re.sub(r"https?://\S+", "", norm(value))
    text = re.sub(r"[^a-z0-9\s']", " ", text)
    return " ".join(text.split()[:words])


class _Bag:
    __slots__ = ("pool_key", "remaining", "last", "updated")

    def __init__(self, pool_key, remaining, last):
        self.pool_key = pool_key
        self.remaining: list[str] = remaining
        self.last: str | None = last
        self.updated = time.monotonic()


class ShuffleBag:
    MAX_BAGS = 1000

    def __init__(self, rng: random.Random | None = None):
        self._rng = rng or random.Random()
        self._bags: dict[tuple[str, str], _Bag] = {}

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _dedupe(pool: Iterable[str]) -> list[str]:
        seen, out = set(), []
        for item in pool or []:
            text = str(item if item is not None else "").strip()
            key = norm(text)
            if text and key not in seen:
                seen.add(key)
                out.append(text)
        return out

    def _fresh(self, items: list[str], last: str | None) -> list[str]:
        bag = items[:]
        self._rng.shuffle(bag)
        if last and len(bag) > 1 and norm(bag[0]) == norm(last):
            j = self._rng.randrange(1, len(bag))
            bag[0], bag[j] = bag[j], bag[0]
        return bag

    @staticmethod
    def _first_allowed(remaining: Sequence[str], avoid: set[str]) -> int | None:
        for i, item in enumerate(remaining):
            if norm(item) not in avoid:
                return i
        return None

    def _prune(self) -> None:
        if len(self._bags) <= self.MAX_BAGS:
            return
        cutoff = time.monotonic() - 30 * 60
        for key in [k for k, b in self._bags.items() if b.updated < cutoff]:
            del self._bags[key]
        if len(self._bags) > self.MAX_BAGS:  # still too many: drop the oldest
            for key, _ in sorted(self._bags.items(), key=lambda kv: kv[1].updated)[: len(self._bags) - self.MAX_BAGS]:
                del self._bags[key]

    # -- public ----------------------------------------------------------
    def pick(self, scope, name: str, pool: Iterable[str], avoid: Iterable[str] = ()) -> str | None:
        items = self._dedupe(pool)
        if not items:
            return None

        key = (str(scope), str(name))
        pool_key = hash(tuple(norm(i) for i in items))
        bag = self._bags.get(key)
        if bag is None or bag.pool_key != pool_key:  # new or edited pool -> new rotation
            bag = _Bag(pool_key, self._fresh(items, bag.last if bag else None), bag.last if bag else None)
        elif not bag.remaining:
            bag.remaining = self._fresh(items, bag.last)

        avoid_set = {norm(a) for a in avoid}
        idx = self._first_allowed(bag.remaining, avoid_set)
        if idx is None and avoid_set:
            # everything left was said recently -> start the next rotation early
            bag.remaining = self._fresh(items, bag.last)
            idx = self._first_allowed(bag.remaining, avoid_set)
        if idx is None:
            # pool is smaller than the memory window: only refuse an immediate repeat
            idx = self._first_allowed(bag.remaining, {norm(bag.last)} if bag.last else set())
            if idx is None:
                idx = 0

        choice = bag.remaining.pop(idx)
        bag.last = choice
        bag.updated = time.monotonic()
        self._bags[key] = bag
        self._prune()
        return choice

    def clear(self, scope=None) -> None:
        if scope is None:
            self._bags.clear()
            return
        for key in [k for k in self._bags if k[0] == str(scope)]:
            del self._bags[key]


class ReplyMemory:
    """What did we recently say in this thread? Wraps a ShuffleBag."""

    def __init__(self, bag: ShuffleBag | None = None, max_recent: int | None = None, ttl_s: float | None = None):
        self.bag = bag or ShuffleBag()
        self.max_recent = max_recent or config.MEMORY_SIZE
        self.ttl_s = ttl_s or config.MEMORY_TTL_S
        self._threads: dict[str, dict] = {}

    def _state(self, thread_id) -> dict:
        key, now = str(thread_id or "unknown"), time.monotonic()
        state = self._threads.get(key)
        if state is None or now - state["at"] > self.ttl_s:
            state = {"at": now, "replies": [], "sigs": []}
            self._threads[key] = state
        state["at"] = now
        if len(self._threads) > 1000:
            for k in [k for k, v in self._threads.items() if now - v["at"] > self.ttl_s]:
                del self._threads[k]
        return state

    def recent(self, thread_id) -> list[str]:
        return list(self._state(thread_id)["replies"])

    def remember(self, thread_id, reply: str) -> None:
        text = str(reply or "").strip()
        if not text:
            return
        state = self._state(thread_id)
        sig = signature(text)
        state["replies"] = [r for r in state["replies"] if r != text] + [text]
        state["replies"] = state["replies"][-self.max_recent:]
        if sig:
            state["sigs"] = [s for s in state["sigs"] if s != sig] + [sig]
            state["sigs"] = state["sigs"][-self.max_recent:]

    def is_recent(self, thread_id, reply: str) -> bool:
        state = self._state(thread_id)
        return reply in state["replies"] or signature(reply) in state["sigs"]

    def pick(self, thread_id, pool_name: str, pool: Sequence[str], remember: bool = True) -> str | None:
        state = self._state(thread_id)
        sigs = set(state["sigs"])
        # keep the WHOLE pool attached to one bag (so the rotation is never reset) and just
        # tell the bag which lines to skip right now.
        avoid = list(state["replies"]) + [p for p in pool if signature(p) in sigs]
        choice = self.bag.pick(thread_id, pool_name, pool, avoid=avoid)
        if choice and remember:
            self.remember(thread_id, choice)
        return choice
