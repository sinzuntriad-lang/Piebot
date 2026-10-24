"""Banat Messenger bot - main engine.

One process runs everything (this is what Railway starts):
    python main.py
      |- BotEngine supervisor : login -> listen -> auto-reconnect (exponential backoff + watchdog)
      |- web dashboard        : FastAPI served by uvicorn (/health, UI, control API)

Rules the engine follows:
  * it ONLY answers in GCs that are in the target list (dashboard, or `!banat on` by the owner)
  * it never answers itself
  * a dead cookie (checkpoint / logged out) is NOT retried in a loop - it waits for a new cookie
"""
from __future__ import annotations

import asyncio
import collections
import logging
import random
import re
import time
from datetime import datetime, timezone

import config
from gc_cycle import GcCycle
from humanizer import HumanSender
from memory import ReplyMemory
from messenger import AuthError, ConnectError, IncomingMessage, MessengerClient, classify_error, create_client
from store import Store
from traffic import ThrottledError, TrafficGate, is_throttle_error
from triggers import TriggerEngine

log = logging.getLogger("banat")
COMMAND_RE = re.compile(r"^!(banat|cycle)(?:\s|$)", re.I)


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BotEngine:
    def __init__(self, store: Store | None = None, client_factory=None, rng: random.Random | None = None):
        self.store = store or Store()
        self.client_factory = client_factory or (lambda: create_client(config.MESSENGER_BACKEND))
        self.rng = rng or random.Random()

        self.memory = ReplyMemory()
        self.triggers = TriggerEngine(self.memory, self.rng)
        self.gate = TrafficGate()
        self.sender = HumanSender(self._set_typing, self.rng)
        self.cycle = GcCycle(
            get_client=lambda: self.client if self.connected else None,
            push_event=self.push_event, note_error=self.note_error, is_target=self.store.is_target,
        )

        # connection state
        self.client: MessengerClient | None = None
        self.connected = False
        self.uid: str | None = None
        self.login_error: str | None = None
        self.needs_cookie = False
        self.reconnect_attempts = 0
        self.next_retry_at: str | None = None
        self.last_connected_at: str | None = None
        self.last_disconnected_at: str | None = None
        self.last_message_at: str | None = None
        self.last_probe_ok: bool | None = None
        self.last_error: dict | None = None
        self._connected_since: float | None = None
        self.started_at = time.monotonic()

        self.stats = {"received": 0, "sent": 0, "commands": 0, "errors": 0, "skipped": 0}
        self.events: collections.deque = collections.deque(maxlen=200)

        self._wake = asyncio.Event()
        self._reset_backoff = False
        self._stop_session: asyncio.Event | None = None
        self._shutdown = False
        self._supervisor: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()

    # ===================================================================== bookkeeping
    def push_event(self, kind: str, message: str) -> None:
        self.events.appendleft({"type": kind, "message": str(message)[:300], "at": _iso()})

    def note_error(self, where: str, error) -> None:
        self.stats["errors"] += 1
        text = str(error)[:300]
        message = f"{type(error).__name__}: {text}" if isinstance(error, BaseException) else text
        self.last_error = {"where": where, "message": message, "at": _iso()}
        self.push_event("error", f"{where}: {self.last_error['message']}")
        log.warning("%s: %s", where, self.last_error["message"])

    # ===================================================================== lifecycle
    def start(self) -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _l, ctx: self.note_error("asyncio", ctx.get("exception") or ctx.get("message")))
        self._supervisor = asyncio.create_task(self._supervise(), name="supervisor")

    async def shutdown(self) -> None:
        self._shutdown = True
        self.request_session_stop()
        self._wake.set()
        if self._supervisor:
            self._supervisor.cancel()
            await asyncio.gather(self._supervisor, return_exceptions=True)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.cycle.stop_all()

    def request_session_stop(self) -> None:
        if self._stop_session:
            self._stop_session.set()

    def wake(self, reset_backoff: bool = True) -> None:
        self._reset_backoff = self._reset_backoff or reset_backoff
        self._wake.set()

    async def _sleep_until_wake(self, timeout: float | None) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    # ===================================================================== supervisor
    async def _supervise(self) -> None:
        attempts = 0
        while not self._shutdown:
            if self._reset_backoff:
                attempts, self._reset_backoff = 0, False
                self.reconnect_attempts, self.next_retry_at = 0, None
                self._wake.clear()

            if not self.store.enabled or not self.store.has_session() or self.needs_cookie:
                self.next_retry_at = None
                await self._sleep_until_wake(None)
                continue

            started = time.monotonic()
            reason = "connection closed"
            try:
                await self._session()
                if self._shutdown:
                    break
                continue_now = not self.store.enabled or self._reset_backoff or self._wake.is_set()
                if continue_now:
                    continue
            except asyncio.CancelledError:
                raise
            except AuthError as exc:
                self.login_error = str(exc)[:300]
                self.needs_cookie = True
                self.note_error("login", exc)
                self.push_event("watchdog", "Session rejected by Facebook - paste a fresh cookie in the dashboard.")
                continue
            except Exception as exc:
                reason = str(classify_error(exc))[:200]
                self.login_error = reason
                self.note_error("session", exc)

            if time.monotonic() - started > 120:  # it was healthy for a while -> start backoff over
                attempts = 0
            if attempts >= config.MAX_RECONNECTS:
                self.push_event("watchdog", f"Gave up after {attempts} tries. Retrying in "
                                            f"{int(config.RETRY_AFTER_GIVE_UP_S // 60)} min, or paste a fresh cookie.")
                self.next_retry_at = datetime.fromtimestamp(time.time() + config.RETRY_AFTER_GIVE_UP_S, timezone.utc).isoformat(timespec="seconds")
                await self._sleep_until_wake(config.RETRY_AFTER_GIVE_UP_S)
                attempts = 0
                continue

            delay = min(300, 5 * 2 ** attempts)
            attempts += 1
            self.reconnect_attempts = attempts
            self.next_retry_at = datetime.fromtimestamp(time.time() + delay, timezone.utc).isoformat(timespec="seconds")
            self.push_event("watchdog", f"{reason} -> reconnect #{attempts} in {int(delay)}s")
            await self._sleep_until_wake(delay)
            self.next_retry_at = None

    async def _session(self) -> None:
        client = self.client_factory()
        self._stop_session = stop = asyncio.Event()
        try:
            try:
                await asyncio.wait_for(client.login(self.store.cookie_file), config.LOGIN_TIMEOUT_S)
            except asyncio.TimeoutError as exc:
                raise ConnectError("login timed out") from exc
            except Exception as exc:
                raise classify_error(exc) from exc

            self.client, self.uid = client, str(client.uid or "")
            self.connected, self._connected_since = True, time.monotonic()
            self.login_error, self.needs_cookie = None, False
            self.last_connected_at = _iso()
            self.push_event("connection", f"Messenger connected as {self.uid} ({client.name})")
            log.info("online as %s via %s", self.uid, client.name)

            tasks = {
                asyncio.create_task(client.listen(self._on_message), name="listen"),
                asyncio.create_task(self._probe_loop(client), name="probe"),
                asyncio.create_task(stop.wait(), name="stop"),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if task.get_name() != "stop" and not task.cancelled() and task.exception():
                    raise classify_error(task.exception())
            if not stop.is_set():
                raise ConnectError("listener ended")
        finally:
            was_connected = self.connected
            self.connected, self.client, self._stop_session = False, None, None
            await self.cycle.stop_all()
            try:
                await asyncio.wait_for(client.close(), 15)
            except Exception as exc:
                log.debug("close failed: %s", exc)
            if was_connected:
                self.last_disconnected_at = _iso()
                self.push_event("connection", "Messenger disconnected")

    async def _probe_loop(self, client: MessengerClient) -> None:
        """Every few minutes ask Messenger 'am I still logged in?'. 3 failures -> reconnect."""
        failures = 0
        while True:
            await asyncio.sleep(config.PROBE_INTERVAL_S)
            try:
                await asyncio.wait_for(client.ping(), 30)
                failures, self.last_probe_ok = 0, True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                self.last_probe_ok = False
                self.note_error("watchdog probe", exc)
                err = classify_error(exc)
                if isinstance(err, AuthError):
                    raise err
                if failures >= 3:
                    raise ConnectError("probe failed 3 times")

    # ===================================================================== incoming
    def _on_message(self, msg: IncomingMessage) -> None:
        """Called by the Messenger backend for EVERY message. Must return immediately."""
        if self._shutdown:
            return
        task = asyncio.create_task(self._handle(msg))
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception():
            self.note_error("message handler", task.exception())

    def is_owner(self, sender_id) -> bool:
        sid = str(sender_id or "")
        return bool(sid) and (sid == self.uid or sid in config.OWNER_IDS)

    async def _handle(self, msg: IncomingMessage) -> None:
        thread, body = str(msg.thread_id), (msg.text or "").strip()
        self.cycle.observe(thread, msg.sender_id)
        if not body:
            return

        m = COMMAND_RE.match(body)
        if m:
            await self._command(msg, thread, body, m.group(1).lower())
            return

        if self.uid and str(msg.sender_id) == self.uid:
            return  # never answer ourselves
        if not self.store.enabled or not self.store.is_target(thread):
            return  # GC not activated -> stay silent

        self.stats["received"] += 1
        self.last_message_at = _iso()
        self.push_event("message", f"{thread} · {msg.sender_id}: {body[:60]}")

        reply = self.triggers.reply_for(thread, body, fallback=self.rng.random() < self.store.fallback_chance)
        if reply:
            await self._reply(thread, reply.text, body, msg.message_id, reply.source)

    # ===================================================================== sending
    async def _set_typing(self, thread_id: str, on: bool) -> None:
        client = self.client
        if client:
            await client.set_typing(thread_id, on)

    @staticmethod
    async def _send_text(client: MessengerClient, thread: str, text: str, reply_to: str | None):
        try:
            return await client.send_message(thread, text, reply_to)
        except Exception as exc:
            if reply_to and not is_throttle_error(exc):  # quote-reply failed -> plain message
                return await client.send_message(thread, text, None)
            raise

    async def _reply(self, thread: str, text: str, incoming: str, reply_to: str | None = None, label: str = "reply") -> bool:
        if not self.gate.try_reserve(thread):
            self.stats["skipped"] += 1  # GC already has replies queued; don't pile up
            return False
        try:
            async with self.gate.lock(thread):
                client = self.client
                if client is None:
                    return False
                prepared = self.sender.prepare(thread, text, incoming)

                async def send(final: str):
                    return await self.gate.send(thread, lambda: self._send_text(client, thread, final, reply_to))

                await self.sender.deliver(thread, prepared, send)
                self.stats["sent"] += 1
                self.push_event("reply", f"{thread} [{label}/{prepared.social}] {prepared.text[:70]}")
                return True
        except ThrottledError as exc:
            self.push_event("throttle", f"{thread}: {exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.note_error("reply", exc)
        finally:
            self.gate.release(thread)
        return False

    # ===================================================================== chat commands (owner only)
    async def _command(self, msg: IncomingMessage, thread: str, body: str, kind: str) -> None:
        if not self.is_owner(msg.sender_id):
            if self.store.is_target(thread):  # only mock people inside activated GCs
                line = self.triggers.command_reply("unauthorized", thread)
                if line:
                    await self._reply(thread, line, body, msg.message_id, "unauthorized")
            return

        self.stats["commands"] += 1
        parts = body.split()
        sub = (parts[1] if len(parts) > 1 else "status").lower()

        async def say(text: str):
            await self._reply(thread, text, body, msg.message_id, "command")

        if kind == "banat":
            if sub in ("on", "enable", "start"):
                self.store.add_target(thread, by=str(msg.sender_id))
                await say(self.triggers.command_reply("on", thread) or "banat is on")
            elif sub in ("off", "disable", "stop"):
                self.store.remove_target(thread)
                await self.cycle.stop(thread)
                await say(self.triggers.command_reply("off", thread) or "banat off")
            elif sub == "toggle":
                if self.store.is_target(thread):
                    self.store.remove_target(thread)
                    await self.cycle.stop(thread)
                    await say("banat is off")
                else:
                    self.store.add_target(thread, by=str(msg.sender_id))
                    await say("banat is on 😭")
            elif sub == "status":
                await say("banat: ON 🟢" if self.store.is_target(thread) else "banat: OFF 🔴")
            else:
                # NOTE: must not START with "!" or the bot would read its own help text as a command
                await say("commands: !banat on / off / toggle / status, !cycle on / off / status")
            return

        # !cycle on [seconds] | off | status
        try:
            if sub in ("on", "start"):
                if not self.store.is_target(thread):
                    await say("i-on mo muna ang banat dito (!banat on)")
                    return
                raw = float(parts[2]) if len(parts) > 2 else 3.0
                info = await self.cycle.start(thread, interval_s=raw / 1000 if raw > 100 else raw)
                await say(f"cycle on ({info['lines']} lines, {info['members']} members)")
            elif sub in ("off", "stop"):
                await say("cycle off" if await self.cycle.stop(thread) else "walang tumatakbong cycle dito")
            else:
                job = next((j for j in self.cycle.status() if j["thread_id"] == thread), None)
                await say(f"cycle ON: step {job['step']}/{job['total']}, {job['members']} members" if job else "cycle OFF")
        except Exception as exc:
            self.note_error("cycle command", exc)
            await say("cycle error: " + str(exc)[:120])

    # ===================================================================== dashboard-facing API
    def status(self) -> dict:
        now = time.monotonic()
        return {
            "ok": True,
            "backend": config.MESSENGER_BACKEND,
            "facebook_connected": self.connected,
            "facebook_user_id": self.uid,
            "bot_enabled": self.store.enabled,
            "bot_running": self.connected and self.store.enabled,
            "needs_cookie": self.needs_cookie,
            "session_saved": self.store.has_session(),
            "login_error": self.login_error,
            "last_error": self.last_error,
            "last_connected_at": self.last_connected_at,
            "last_disconnected_at": self.last_disconnected_at,
            "last_message_at": self.last_message_at,
            "bot_uptime_seconds": int(now - self._connected_since) if self.connected and self._connected_since else 0,
            "server_uptime_seconds": int(now - self.started_at),
            "reconnect_attempts": self.reconnect_attempts,
            "next_retry_at": self.next_retry_at,
            "last_probe_ok": self.last_probe_ok,
            "messages_received": self.stats["received"],
            "replies_sent": self.stats["sent"],
            "commands_used": self.stats["commands"],
            "replies_skipped_busy": self.stats["skipped"],
            "errors": self.stats["errors"],
            "target_gcs": len(self.store.targets),
            "threads_in_cooldown": self.gate.cooling_down(),
            "fallback_chance": self.store.fallback_chance,
            "cycles": self.cycle.status(),
        }

    def start_bot(self) -> None:
        self.store.update_settings(enabled=True)
        self.needs_cookie = False
        self.wake()

    def stop_bot(self) -> None:
        self.store.update_settings(enabled=False)
        self.request_session_stop()
        self.wake()

    def reconnect(self) -> None:
        self.store.update_settings(enabled=True)
        self.needs_cookie = False
        self.request_session_stop()
        self.wake()

    def connect_session(self, raw) -> dict:
        info = self.store.save_session(raw)  # raises SessionError on a bad cookie
        self.store.update_settings(enabled=True)
        self.needs_cookie, self.login_error = False, None
        self.push_event("session", f"Cookie saved for {info['account']}")
        self.request_session_stop()
        self.wake()
        return info

    def clear_session(self) -> None:
        self.store.clear_session()
        self.request_session_stop()
        self.wake()
        self.push_event("session", "Cookie removed")

    async def set_targets(self, thread_ids) -> list[str]:
        before = set(self.store.targets)
        ids = self.store.set_targets(thread_ids)
        for removed in before - set(ids):
            await self.cycle.stop(removed)
        self.push_event("targets", f"Target GCs: {', '.join(ids) or 'none'}")
        return ids

    async def add_target(self, thread_id) -> str:
        tid = self.store.add_target(thread_id)
        self.push_event("targets", f"ON {tid}")
        return tid

    async def remove_target(self, thread_id) -> bool:
        await self.cycle.stop(thread_id)
        removed = self.store.remove_target(thread_id)
        self.push_event("targets", f"OFF {thread_id}")
        return removed


# ========================================================================= entry point
async def amain() -> None:
    import uvicorn

    from dashboard import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if config.DASHBOARD_KEY_GENERATED:
        log.warning("DASHBOARD_API_KEY is not set. Temporary dashboard key for this run: %s  "
                    "(set DASHBOARD_API_KEY in Railway so it stays the same)", config.DASHBOARD_KEY)

    engine = BotEngine()
    engine.start()
    server = uvicorn.Server(uvicorn.Config(
        create_app(engine), host=config.HOST, port=config.PORT, log_level="info",
        proxy_headers=True, forwarded_allow_ips="*",
    ))
    try:
        await server.serve()  # returns after SIGTERM/SIGINT (uvicorn handles the signals)
    finally:
        await engine.shutdown()


if __name__ == "__main__":
    asyncio.run(amain())
