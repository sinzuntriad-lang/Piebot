"""Central configuration. Everything can be overridden with environment variables."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "data"


def _num(name: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
    try:
        value = float(os.getenv(name, default))
    except (TypeError, ValueError):
        value = float(default)
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- paths
# On Railway attach a Volume (e.g. mount path /data) so cookies/targets survive redeploys.
DATA_DIR = Path(os.getenv("DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or (BASE_DIR / "state"))
ROASTS_FILE = Path(os.getenv("ROASTS_FILE") or (ASSETS_DIR / "roasts.txt"))

# --------------------------------------------------------------------------- web
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(_num("PORT", 8000, 1, 65535))  # Railway injects PORT

# Dashboard password. If it is not set a random one is generated on every boot and
# printed in the logs - set DASHBOARD_API_KEY in Railway so it stays the same.
_env_key = (os.getenv("DASHBOARD_API_KEY") or "").strip()
DASHBOARD_KEY_GENERATED = not _env_key
DASHBOARD_KEY = _env_key or secrets.token_urlsafe(18)

# --------------------------------------------------------------------------- bot
MESSENGER_BACKEND = os.getenv("MESSENGER_BACKEND", "muqit").strip().lower()  # muqit | fake
OWNER_IDS = {x.strip() for x in os.getenv("BANAT_OWNER_IDS", "").split(",") if x.strip()}
ENV_TARGETS = [x.strip() for x in os.getenv("BANAT_TARGETS", "").split(",") if x.strip()]

# Chance (0-1) to answer a message in a target GC that matched NO trigger.
# Triggered messages are always answered. Editable from the dashboard.
FALLBACK_CHANCE = _num("BANAT_FALLBACK_CHANCE", 0.5, 0, 1)

# --------------------------------------------------------------------------- humanizer
SPEED = _num("BANAT_SPEED", 0.6, 0.2, 2)            # <1 = faster typing/reaction
MAX_DELAY_MS = _num("BANAT_MAX_HUMAN_DELAY_MS", 2800, 1200, 20000)
ABBREV_RATE = _num("BANAT_SHORTHAND_RATE", 0.4, 0, 1)  # chance a reply gets shorthand
TYPO_RATE = _num("BANAT_TYPO_RATE", 0.085, 0, 1)
CASE_RATE = _num("BANAT_CASE_VARIATION_RATE", 0.035, 0, 1)
SPACE_RATE = _num("BANAT_SPACE_SLIP_RATE", 0.018, 0, 1)
HESITATION_RATE = _num("BANAT_HESITATION_RATE", 0.11, 0, 0.35)
TYPING_REFRESH_S = _num("TYPING_REFRESH_S", 3.5, 1.5, 10)

# --------------------------------------------------------------------------- memory
MEMORY_SIZE = int(_num("BANAT_MEMORY_SIZE", 36, 20, 60))
MEMORY_TTL_S = _num("BANAT_MEMORY_TTL_S", 20 * 60, 60)

# --------------------------------------------------------------------------- traffic
GLOBAL_SEND_LIMIT = int(_num("BANAT_GLOBAL_SEND_LIMIT", 2, 1, 10))
THREAD_COOLDOWN_S = _num("BANAT_THREAD_COOLDOWN_S", 2.5, 0)
MAX_PENDING_PER_THREAD = int(_num("BANAT_MAX_PENDING", 2, 1, 10))  # extra messages are skipped

# --------------------------------------------------------------------------- reconnect / watchdog
MAX_RECONNECTS = int(_num("BANAT_MAX_RECONNECTS", 10, 1))
PROBE_INTERVAL_S = _num("BANAT_PROBE_INTERVAL_S", 120, 30)
LOGIN_TIMEOUT_S = _num("BANAT_LOGIN_TIMEOUT_S", 60, 10)
RETRY_AFTER_GIVE_UP_S = _num("BANAT_RETRY_AFTER_GIVE_UP_S", 30 * 60, 60)

# --------------------------------------------------------------------------- gc cycle
CYCLE_MIN_STEP_S = _num("CYCLE_MIN_STEP_S", 2.5, 1.5)
CYCLE_MIN_NICK_S = _num("CYCLE_MIN_NICK_S", 1.5, 1.0)
CYCLE_CALL_TIMEOUT_S = _num("CYCLE_CALL_TIMEOUT_S", 8, 3)
CYCLE_MAX_FAILS = int(_num("CYCLE_MAX_FAILS", 6, 3))
