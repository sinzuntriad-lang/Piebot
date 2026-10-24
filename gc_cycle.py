"""GC Cycle: auto-change the GC title and every member's nickname, one line of roasts.txt per round.

Design (same fixes as the old JS version):
  - sequential loop per GC, no overlapping rounds
  - every Messenger call has a timeout, so the loop can never hang
  - members come from the thread info AND from anyone seen chatting (late joiners)
  - auto-stops after several rounds of pure errors (usually Facebook rate-limiting)
  - jittered pacing, so it is not a metronome
Only GCs that are in the target list can run a cycle.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import config

log = logging.getLogger("gc_cycle")


def load_lines(path: Path | None = None) -> list[str]:
    """One line = one step. Blank lines and lines starting with # are ignored."""
    path = Path(path or config.ROASTS_FILE)
    if not path.exists():
        raise FileNotFoundError(f"roasts file not found: {path}")
    lines = [ln.strip() for ln in path.read_text(encoding="utf8").splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if not lines:
        raise ValueError(f"{path.name} is empty - put one line per step in it")
    return lines


def _jitter(seconds: float, pct: float = 0.25) -> float:
    return max(0.0, seconds * (1 + random.uniform(-pct, pct)))


@dataclass
class _Job:
    thread_id: str
    lines: list[str]
    do_name: bool
    do_nick: bool
    interval_s: float
    members: set[str] = field(default_factory=set)
    index: int = 0
    step: int = 0
    nick_calls: int = 0
    note: str = ""
    started: float = field(default_factory=time.monotonic)
    task: asyncio.Task | None = None
    on: bool = True


class GcCycle:
    def __init__(self, get_client: Callable[[], object | None],
                 push_event: Callable[[str, str], None] = lambda t, m: None,
                 note_error: Callable[[str, object], None] = lambda w, e: None,
                 is_target: Callable[[str], bool] = lambda t: True):
        self._get_client = get_client
        self._push = push_event
        self._note_error = note_error
        self._is_target = is_target
        self._jobs: dict[str, _Job] = {}

    # -- calls with timeout -----------------------------------------------
    async def _call(self, coro: Awaitable) -> tuple[bool, Exception | None]:
        try:
            await asyncio.wait_for(coro, config.CYCLE_CALL_TIMEOUT_S)
            return True, None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # includes TimeoutError
            return False, exc

    async def _members(self, client, thread_id: str) -> set[str]:
        try:
            return set(await asyncio.wait_for(client.get_participants(thread_id), config.CYCLE_CALL_TIMEOUT_S))
        except Exception as exc:
            log.warning("could not fetch members of %s: %s", thread_id, exc)
            return set()

    def observe(self, thread_id: str, *user_ids: str | None) -> None:
        """Feed it chat activity so people who join later are picked up too."""
        job = self._jobs.get(str(thread_id))
        if job:
            job.members.update(str(u) for u in user_ids if u)

    # -- the loop -------------------------------------------------------------
    async def _run(self, job: _Job) -> None:
        fails = 0
        try:
            while job.on:
                client = self._get_client()
                if client is None:  # reconnecting - wait, don't die
                    await asyncio.sleep(3)
                    continue

                if job.index >= len(job.lines):
                    job.index = 0
                text = job.lines[job.index]
                job.step = job.index + 1
                step_fails = 0

                if job.do_name:
                    ok, err = await self._call(client.set_thread_name(job.thread_id, text))
                    if not ok:
                        step_fails += 1
                        self._note_error("cycle gc-name", err)

                if job.do_nick:
                    for uid in list(job.members):
                        if not job.on:
                            break
                        ok, err = await self._call(client.set_nickname(job.thread_id, uid, text))
                        if not ok:
                            step_fails += 1
                            self._note_error("cycle nickname", err)
                        job.nick_calls += 1
                        await asyncio.sleep(_jitter(config.CYCLE_MIN_NICK_S))

                fails = fails + 1 if step_fails else 0
                if fails >= config.CYCLE_MAX_FAILS:
                    job.on = False
                    job.note = "auto-stopped: puro error (posibleng rate-limit). Maghintay muna bago ulit."
                    log.warning("[%s] %s", job.thread_id, job.note)
                    self._push("cycle", f"{job.thread_id}: {job.note}")
                    break

                job.index += 1
                await asyncio.sleep(_jitter(max(job.interval_s, config.CYCLE_MIN_STEP_S)) + random.uniform(0.1, 0.9))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._note_error("cycle", exc)
        finally:
            job.on = False
            if self._jobs.get(job.thread_id) is job and not job.note:
                self._jobs.pop(job.thread_id, None)

    # -- public ------------------------------------------------------------------
    async def start(self, thread_id, *, lines: list[str] | None = None, do_name: bool = True,
                    do_nick: bool = True, interval_s: float = 3.0) -> dict:
        tid = str(thread_id or "").strip()
        if not tid:
            raise ValueError("Missing thread ID")
        if not self._is_target(tid):
            raise ValueError("Hindi target GC ito. Idagdag muna sa Target GCs.")
        client = self._get_client()
        if client is None:
            raise RuntimeError("Not connected to Messenger")
        existing = self._jobs.get(tid)
        if existing and existing.on:
            raise RuntimeError("Cycle already running in this thread")
        if not do_name and not do_nick:
            raise ValueError("Enable at least GC name or nickname")

        cleaned = [str(x).strip() for x in (lines or []) if str(x).strip() and not str(x).strip().startswith("#")]
        job = _Job(tid, cleaned or load_lines(), do_name, do_nick, max(config.CYCLE_MIN_STEP_S, float(interval_s)))
        job.members = await self._members(client, tid)
        self._jobs[tid] = job
        job.task = asyncio.create_task(self._run(job), name=f"gc-cycle-{tid}")
        log.info("[%s] cycle started (%d lines, %d members)", tid, len(job.lines), len(job.members))
        self._push("cycle", f"started in {tid} ({len(job.lines)} lines)")
        return {"thread_id": tid, "lines": len(job.lines), "members": len(job.members)}

    async def stop(self, thread_id) -> bool:
        job = self._jobs.pop(str(thread_id), None)
        if not job:
            return False
        job.on = False
        if job.task:
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
        self._push("cycle", f"stopped in {thread_id}")
        return True

    async def stop_all(self) -> None:
        for tid in list(self._jobs):
            await self.stop(tid)

    def status(self) -> list[dict]:
        return [{
            "thread_id": j.thread_id, "running": j.on, "step": j.step, "total": len(j.lines),
            "members": len(j.members), "nickname_calls": j.nick_calls, "note": j.note or None,
            "uptime_s": int(time.monotonic() - j.started),
        } for j in self._jobs.values()]

    def is_running(self, thread_id) -> bool:
        job = self._jobs.get(str(thread_id))
        return bool(job and job.on)
