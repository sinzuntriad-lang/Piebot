"""Small JSON persistence: target GCs, settings and the Facebook session cookie.

Everything lives in DATA_DIR. On Railway attach a Volume and either mount it at /data
(or set DATA_DIR) so these files survive redeploys.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import config

log = logging.getLogger("store")
THREAD_ID_RE = re.compile(r"^\d{5,}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SessionError(ValueError):
    pass


def parse_cookies(raw: str | list | dict) -> list[dict]:
    """Accept a JSON array / {"cookies": [...]} / {"appState": [...]} or a 'a=b; c=d' string.

    Returns a normalized list of cookie dicts. Raises SessionError if c_user / xs are missing.
    """
    entries: list[dict] = []
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise SessionError("Walang laman ang cookie.")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
            for part in text.split(";"):
                name, sep, value = part.partition("=")
                if sep and name.strip():
                    entries.append({"name": name.strip(), "value": value.strip()})
    if data is not None and not entries:
        if isinstance(data, dict):
            data = data.get("cookies") or data.get("appState") or data.get("appstate")
        if not isinstance(data, list):
            raise SessionError("Cookie must be a JSON array, or a 'c_user=...; xs=...' string.")
        for item in data:
            if isinstance(item, dict):
                name = item.get("name", item.get("key"))
                if name is not None and item.get("value") is not None:
                    entries.append({**item, "name": str(name), "value": str(item["value"])})

    names = {e["name"]: e["value"] for e in entries}
    if not names.get("c_user") or not names.get("xs"):
        raise SessionError("Kulang ang cookie: kailangan may c_user at xs.")

    out = []
    for e in entries:
        out.append({
            "key": e["name"], "name": e["name"], "value": e["value"],
            "domain": e.get("domain") or ".facebook.com", "path": e.get("path") or "/",
            "secure": e.get("secure", True), "httpOnly": e.get("httpOnly", False),
            "hostOnly": e.get("hostOnly", False),
        })
    return out


class Store:
    def __init__(self, data_dir: Path | None = None):
        self.dir = Path(data_dir or config.DATA_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.dir / "state.json"
        self.cookie_file = self.dir / "cookies.json"
        self._lock = threading.RLock()
        self.targets: dict[str, dict] = {}
        self.settings: dict = {"enabled": True, "fallback_chance": config.FALLBACK_CHANCE}
        self.load()

    # ---- state.json -------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            try:
                data = json.loads(self.state_file.read_text(encoding="utf8"))
                self.targets = {str(k): v for k, v in (data.get("targets") or {}).items()}
                self.settings.update(data.get("settings") or {})
            except FileNotFoundError:
                pass
            except Exception as exc:
                log.error("state.json unreadable (%s) - starting clean", exc)
            # BANAT_TARGETS env = extra GCs for hosts with an ephemeral disk
            for tid in config.ENV_TARGETS:
                if THREAD_ID_RE.match(tid):
                    self.targets.setdefault(tid, {"since": None, "by": "env"})
            # FB_COOKIES env = session fallback when no cookie file exists yet
            env_cookie = os.getenv("FB_COOKIES") or os.getenv("FB_APPSTATE")
            if env_cookie and not self.cookie_file.exists():
                try:
                    self.save_session(env_cookie)
                except SessionError as exc:
                    log.error("FB_COOKIES ignored: %s", exc)

    def save(self) -> None:
        with self._lock:
            payload = json.dumps({"targets": self.targets, "settings": self.settings}, indent=2)
            self._atomic_write(self.state_file, payload)

    def _atomic_write(self, path: Path, text: str, mode: int | None = None) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=path.name + ".")
        try:
            with os.fdopen(fd, "w", encoding="utf8") as fh:
                fh.write(text)
            if mode is not None:
                os.chmod(tmp, mode)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- targets ------------------------------------------------------------
    @staticmethod
    def clean_id(value) -> str:
        tid = str(value or "").strip()
        if not THREAD_ID_RE.match(tid):
            raise ValueError(f"Invalid thread ID {tid!r} (numbers only, at least 5 digits)")
        return tid

    def is_target(self, thread_id) -> bool:
        return str(thread_id) in self.targets

    def add_target(self, thread_id, by: str = "dashboard") -> str:
        tid = self.clean_id(thread_id)
        with self._lock:
            self.targets.setdefault(tid, {"since": now_iso(), "by": by})
            self.save()
        return tid

    def remove_target(self, thread_id) -> bool:
        tid = str(thread_id).strip()
        with self._lock:
            existed = self.targets.pop(tid, None) is not None
            self.save()
        return existed

    def set_targets(self, thread_ids) -> list[str]:
        ids = [self.clean_id(t) for t in thread_ids]
        with self._lock:
            old = self.targets
            self.targets = {t: old.get(t) or {"since": now_iso(), "by": "dashboard"} for t in ids}
            self.save()
        return list(self.targets)

    def list_targets(self) -> list[dict]:
        return [{"thread_id": k, **v} for k, v in self.targets.items()]

    # ---- settings -----------------------------------------------------------
    def update_settings(self, **values) -> dict:
        with self._lock:
            self.settings.update({k: v for k, v in values.items() if v is not None})
            self.save()
            return dict(self.settings)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", True))

    @property
    def fallback_chance(self) -> float:
        try:
            return max(0.0, min(1.0, float(self.settings.get("fallback_chance", 0.5))))
        except (TypeError, ValueError):
            return 0.5

    # ---- facebook session -----------------------------------------------------
    def save_session(self, raw) -> dict:
        cookies = parse_cookies(raw)
        with self._lock:
            self._atomic_write(self.cookie_file, json.dumps(cookies), mode=0o600)
        return self.session_info()

    def has_session(self) -> bool:
        return self.cookie_file.exists()

    def clear_session(self) -> None:
        try:
            self.cookie_file.unlink()
        except FileNotFoundError:
            pass

    def session_info(self) -> dict:
        """Never returns the cookie itself - only the account id and save time."""
        try:
            cookies = json.loads(self.cookie_file.read_text(encoding="utf8"))
            account = next((c["value"] for c in cookies if c["name"] == "c_user"), None)
            saved_at = datetime.fromtimestamp(self.cookie_file.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
            return {"saved": True, "account": f"c_user {account}" if account else None, "saved_at": saved_at}
        except Exception:
            return {"saved": False, "account": None, "saved_at": None}
