"""Messenger adapter.

The rest of the bot only talks to `MessengerClient`, never to a specific library. That keeps
all the library-specific code in ONE place (MuqitClient), so if the library changes or you
want to try another one you only rewrite that class.

Backends
  muqit : fbchat-muqit  (async, cookie login, MQTT realtime)      <- default
  fake  : in-memory client for testing the dashboard / engine without Facebook
"""
from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("messenger")


# ----------------------------------------------------------------------------- types
@dataclass
class IncomingMessage:
    thread_id: str
    sender_id: str
    text: str
    message_id: str | None = None
    reply_to_sender_id: str | None = None


class MessengerError(Exception):
    """Base error for anything the backend raises."""


class AuthError(MessengerError):
    """Cookie is dead / checkpoint / logged out. Retrying will not help - a new cookie is needed."""


class ConnectError(MessengerError):
    """Network / temporary problem. Worth retrying with backoff."""


_AUTH_RE = re.compile(
    r"checkpoint|not logged in|login (?:approval|required|failed)|session.*(?:expired|invalid)|"
    r"invalid.*(?:session|cookie)|cookie.*(?:expired|invalid)|authentication|unauthori[sz]ed|c_user|\bxs\b",
    re.I,
)


def classify_error(exc: BaseException) -> MessengerError:
    if isinstance(exc, MessengerError):
        return exc
    text = f"{type(exc).__name__}: {exc}"
    return (AuthError if _AUTH_RE.search(text) else ConnectError)(text)


# ----------------------------------------------------------------------------- interface
class MessengerClient(ABC):
    name = "abstract"
    uid: str | None = None

    @abstractmethod
    async def login(self, cookies_path: Path) -> None: ...

    @abstractmethod
    async def listen(self, on_message: Callable[[IncomingMessage], Any]) -> None:
        """Block until the realtime connection ends (return or raise = disconnected)."""

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def send_message(self, thread_id: str, text: str, reply_to: str | None = None) -> str | None: ...

    @abstractmethod
    async def set_typing(self, thread_id: str, on: bool) -> None: ...

    @abstractmethod
    async def set_thread_name(self, thread_id: str, name: str) -> None: ...

    @abstractmethod
    async def set_nickname(self, thread_id: str, user_id: str, nickname: str) -> None: ...

    @abstractmethod
    async def get_participants(self, thread_id: str) -> list[str]: ...

    @abstractmethod
    async def ping(self) -> None:
        """Cheap call that raises if the session is dead (used by the watchdog)."""


# ----------------------------------------------------------------------------- fbchat-muqit
def _first_attr(obj: Any, *names: str, default=None):
    for n in names:
        value = getattr(obj, n, None)
        if value not in (None, ""):
            return value
    return default


def _ids_from(thread: Any) -> list[str]:
    """Pull participant ids out of whatever shape the library's Thread object has."""
    raw = _first_attr(thread, "participant_ids", "participants", "all_participants", "members", "users")
    if isinstance(raw, dict):
        raw = list(raw.keys())
    out = []
    for item in raw or []:
        uid = item if isinstance(item, (str, int)) else _first_attr(item, "id", "uid", "user_id", "fbid")
        if uid is not None:
            out.append(str(uid))
    return out


class MuqitClient(MessengerClient):
    """Adapter for https://pypi.org/project/fbchat-muqit/ (documented API, `fbchat_muqit` module)."""

    name = "fbchat-muqit"

    def __init__(self) -> None:
        self._c: Any = None
        self._handler: Callable[[IncomingMessage], Any] | None = None

    async def login(self, cookies_path: Path) -> None:
        try:
            from fbchat_muqit import Client  # imported lazily so the dashboard still boots if it's missing
        except ImportError as exc:
            raise ConnectError(f"fbchat-muqit is not installed: {exc}") from exc
        try:
            client = Client(cookies_file_path=str(cookies_path))

            @client.event
            async def on_message(message):  # name must be exactly on_message for @client.event
                self._dispatch(message)

            await client.start()  # logs in, does not listen yet
            self._c = client
            self.uid = str(client.uid)
        except Exception as exc:
            raise classify_error(exc) from exc

    def _dispatch(self, m: Any) -> None:
        if not self._handler:
            return
        text = _first_attr(m, "text", "body", default="") or ""
        reply = _first_attr(m, "reply_to_message", "reply_to", "replied_to")
        self._handler(IncomingMessage(
            thread_id=str(_first_attr(m, "thread_id", "threadID", default="")),
            sender_id=str(_first_attr(m, "sender_id", "senderID", "author", default="")),
            text=str(text),
            message_id=_first_attr(m, "id", "message_id", "messageID"),
            reply_to_sender_id=(str(_first_attr(reply, "sender_id", "senderID", "author"))
                                if reply is not None and _first_attr(reply, "sender_id", "senderID", "author") else None),
        ))

    async def listen(self, on_message) -> None:
        self._handler = on_message
        try:
            await self._c.listen()  # blocking; returns/raises when the MQTT loop dies
        except Exception as exc:
            raise classify_error(exc) from exc

    async def close(self) -> None:
        self._handler = None
        if self._c is None:
            return
        for step in ("stop_listening", "close"):
            fn = getattr(self._c, step, None)
            if fn:
                try:
                    await asyncio.wait_for(fn(), 10)
                except Exception as exc:
                    log.debug("%s failed: %s", step, exc)
        self._c = None

    async def send_message(self, thread_id, text, reply_to=None):
        kwargs = {"reply_to_message": str(reply_to)} if reply_to else {}
        result = await self._c.send_message(text, str(thread_id), **kwargs)
        return str(result) if result else None

    async def set_typing(self, thread_id, on):
        await self._c.typing(str(thread_id), bool(on))

    async def set_thread_name(self, thread_id, name):
        await self._c.change_thread_name(str(thread_id), name)

    async def set_nickname(self, thread_id, user_id, nickname):
        await self._c.change_nickname(str(thread_id), str(user_id), nickname)

    async def get_participants(self, thread_id):
        threads = await self._c.fetch_thread_info([str(thread_id)])
        return _ids_from(threads[0]) if threads else []

    async def ping(self):
        await self._c.fetch_user_info(self.uid)


# ----------------------------------------------------------------------------- fake (tests / demo)
class FakeClient(MessengerClient):
    """In-memory client. `inject()` pretends someone sent a message; everything we send is recorded."""

    name = "fake"

    def __init__(self, uid: str = "100000000000001", participants: list[str] | None = None) -> None:
        self.uid = uid
        self.participants = participants or [uid, "100000000000002", "100000000000003"]
        self._queue: asyncio.Queue = asyncio.Queue()
        self.sent: list[tuple[str, str, str | None]] = []
        self.typing_calls: list[tuple[str, bool]] = []
        self.names: list[tuple[str, str]] = []
        self.nicks: list[tuple[str, str, str]] = []
        self.logged_in = False
        self.closed = False
        self.fail_login: BaseException | None = None
        self.fail_sends = 0  # raise this many times before succeeding

    async def login(self, cookies_path):
        if self.fail_login:
            raise self.fail_login
        self.logged_in = True

    async def listen(self, on_message):
        while True:
            item = await self._queue.get()
            if item is None:
                raise ConnectError("fake connection dropped")
            on_message(item)

    def inject(self, msg: IncomingMessage) -> None:
        self._queue.put_nowait(msg)

    def drop(self) -> None:
        self._queue.put_nowait(None)

    async def close(self):
        self.closed = True

    async def send_message(self, thread_id, text, reply_to=None):
        if self.fail_sends > 0:
            self.fail_sends -= 1
            raise RuntimeError("1545012 temporarily unavailable")
        self.sent.append((str(thread_id), text, reply_to))
        return f"mid.{len(self.sent)}"

    async def set_typing(self, thread_id, on):
        self.typing_calls.append((str(thread_id), bool(on)))

    async def set_thread_name(self, thread_id, name):
        self.names.append((str(thread_id), name))

    async def set_nickname(self, thread_id, user_id, nickname):
        self.nicks.append((str(thread_id), str(user_id), nickname))

    async def get_participants(self, thread_id):
        return list(self.participants)

    async def ping(self):
        if self.closed:
            raise ConnectError("closed")


def create_client(backend: str) -> MessengerClient:
    if backend == "fake":
        return FakeClient()
    if backend in ("muqit", "fbchat-muqit", "default"):
        return MuqitClient()
    raise ValueError(f"Unknown MESSENGER_BACKEND {backend!r} (use 'muqit' or 'fake')")
