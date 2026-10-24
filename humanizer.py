"""Make replies look typed by a person: texting shorthand, small slips, and realistic timing.

Three layers, all independent of Messenger itself:
  1. text   : humanize()            shorthand ("bakit" -> "bkt"), typos, casing, spacing, punctuation
  2. timing : calculate_delay()     reaction pause + typing time based on length / mood / momentum
  3. typing : TypingIndicator       keeps the "typing..." bubble alive while we "type"
HumanSender glues them together: pause -> typing bubble -> send.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import config

log = logging.getLogger("humanizer")

# =============================================================================
# 1. TEXT
# =============================================================================
# word/phrase -> possible shorthand. Only a couple are applied per message (see ABBREV_RATE).
ABBREVIATIONS: dict[str, list[str]] = {
    # --- Filipino / Taglish
    "hindi": ["di"], "bakit": ["bkt"], "talaga": ["tlga", "talga"], "kasi": ["ksi"],
    "naman": ["nmn"], "lang": ["lng"], "walang": ["wlang"], "wala": ["wla"],
    "parang": ["prang"], "para": ["pra"], "kapag": ["kpg"], "pag": ["pg"],
    "ganyan": ["gnyan"], "ganun": ["gnun"], "ganoon": ["gnun"], "ngayon": ["ngyon"],
    "mamaya": ["mmya"], "siguro": ["sguro"], "sobrang": ["sbrang"], "gusto": ["gsto"],
    "tayo": ["tyo"], "kayo": ["kyo"], "sino": ["sno"], "saan": ["san"], "yung": ["yng"],
    "ikaw": ["ikw"], "kaya": ["kya"], "sige": ["sge"], "tapos": ["tpos"], "muna": ["mna"],
    "kailangan": ["kelangan"], "nandito": ["ndito"], "pwede": ["pde"], "puwede": ["pde"],
    "dahil": ["dhl"], "mayroon": ["meron"], "ginagawa": ["ngwa"], "nakakatawa": ["nkktwa"],
    "hindi ka": ["di ka"], "hindi mo": ["di mo"], "ano ba": ["anu b"],
    # --- English that shows up in Taglish chat
    "you": ["u"], "your": ["ur"], "you're": ["ur"], "because": ["bc", "cuz"], "please": ["pls", "plz"],
    "people": ["ppl"], "really": ["rly", "rlly"], "right now": ["rn"], "to be honest": ["tbh"],
    "not gonna lie": ["ngl"], "i don't know": ["idk"], "by the way": ["btw"], "of course": ["ofc"],
    "with": ["w/"], "without": ["w/o"], "what": ["wat"], "okay": ["ok", "k"], "thanks": ["thx", "ty"],
    "tomorrow": ["tmrw"], "message": ["msg"], "something": ["smth"], "someone": ["sm1"],
    "everyone": ["evry1"], "about": ["abt"], "though": ["tho"], "laughing": ["lmao"],
    "shut up": ["stfu"], "what the hell": ["wth"], "oh my god": ["omg"], "good luck": ["gl"],
    "good night": ["gn"], "good morning": ["gm"], "tonight": ["tn"], "probably": ["prob"],
}

# longest phrases first so "hindi ka" wins over "hindi"
_ABBREV_PATTERNS = sorted(
    ((re.compile(r"(?<![\w'])" + re.escape(k) + r"(?![\w'])", re.I), v) for k, v in ABBREVIATIONS.items()),
    key=lambda pv: -len(pv[0].pattern),
)

KEYBOARD_NEIGHBORS = {
    "q": "wa", "w": "qase", "e": "wsdr", "r": "edft", "t": "rfgy", "y": "tghu", "u": "yhji",
    "i": "ujko", "o": "iklp", "p": "ol", "a": "qwsz", "s": "awedxz", "d": "serfcx", "f": "drtgvc",
    "g": "ftyhbv", "h": "gyujnb", "j": "huikmn", "k": "jiolm", "l": "kop", "z": "asx", "x": "zsdc",
    "c": "xdfv", "v": "cfgb", "b": "vghn", "n": "bhjm", "m": "njk",
}


def _match_case(original: str, replacement: str) -> str:
    if len(original) > 1 and original.isupper():
        return replacement.upper()
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def apply_abbreviations(text: str, rng: random.Random, rate: float, max_swaps: int = 2) -> str:
    """With probability `rate`, swap 1..max_swaps words/phrases for texting shorthand."""
    if rng.random() >= rate:
        return text
    candidates = [(p, v) for p, v in _ABBREV_PATTERNS if p.search(text)]
    if not candidates:
        return text
    rng.shuffle(candidates)
    for pattern, options in candidates[: rng.randint(1, max_swaps)]:
        text = pattern.sub(lambda m: _match_case(m.group(0), rng.choice(options)), text, count=1)
    return text


def _keyboard_slip(word: str, rng: random.Random) -> str | None:
    spots = [i for i, ch in enumerate(word) if ch.lower() in KEYBOARD_NEIGHBORS]
    if not spots:
        return None
    i = rng.choice(spots)
    repl = rng.choice(KEYBOARD_NEIGHBORS[word[i].lower()])
    return word[:i] + _match_case(word[i], repl) + word[i + 1:]


def _delete_slip(word: str, rng: random.Random) -> str | None:
    if len(word) < 4:
        return None
    i = rng.randint(1, len(word) - 2)
    return word[:i] + word[i + 1:]


def _double_slip(word: str, rng: random.Random) -> str | None:
    if len(word) < 3:
        return None
    i = rng.randint(1, len(word) - 2)
    return word[:i] + word[i] + word[i:]


def _swap_slip(word: str, rng: random.Random) -> str | None:
    if len(word) < 4:
        return None
    i = rng.randint(1, len(word) - 3)
    if word[i] == word[i + 1]:
        return None
    return word[:i] + word[i + 1] + word[i] + word[i + 2:]


_TYPO_STRATEGIES = (_keyboard_slip, _keyboard_slip, _keyboard_slip, _delete_slip, _swap_slip, _double_slip)


def add_typo(text: str, rng: random.Random, rate: float) -> str:
    if rng.random() >= rate:
        return text
    parts = re.split(r"(\s+)", text)
    eligible = [i for i, p in enumerate(parts) if p.strip() and len(re.sub(r"[^A-Za-z]", "", p)) >= 4 and "/" not in p]
    if not eligible:
        return text
    idx = rng.choice(eligible)
    m = re.match(r"^([^A-Za-z]*)([A-Za-z]+)([^A-Za-z]*)$", parts[idx])
    if not m:
        return text
    prefix, word, suffix = m.groups()
    for _ in range(5):
        changed = rng.choice(_TYPO_STRATEGIES)(word, rng)
        if changed and changed != word:
            parts[idx] = prefix + changed + suffix
            return "".join(parts)
    return text


def add_case_variation(text: str, rng: random.Random, rate: float) -> str:
    if len(text) < 10 or rng.random() >= rate:
        return text
    parts = re.split(r"(\s+)", text)
    eligible = [i for i, p in enumerate(parts) if re.fullmatch(r"[A-Za-z]{3,}", p)]
    if not eligible:
        return text
    i = rng.choice(eligible)
    parts[i] = parts[i].lower() if rng.random() < 0.7 else parts[i].capitalize()
    return "".join(parts)


def add_space_slip(text: str, rng: random.Random, rate: float) -> str:
    if len(text) < 16 or rng.random() >= rate:
        return text
    words = text.split()
    if len(words) < 3:
        return text
    i = rng.randint(1, len(words) - 2)
    if rng.random() < 0.7:           # forgot the space
        words[i - 1] += words[i]
        del words[i]
        return " ".join(words)
    words[i - 1] += "  " + words[i]  # double space
    del words[i]
    return " ".join(words)


def add_casual_punctuation(text: str, rng: random.Random) -> str:
    out = text.strip()
    if not out:
        return out
    if rng.random() < 0.12:
        out = re.sub(r"\.$", "", out)
    if rng.random() < 0.08 and not re.search(r"[!?…]$", out):
        out += "..."
    if rng.random() < 0.14:
        out = re.sub(r"\.(?=\s*$)", "", out)
    out = re.sub(r"!{3,}", lambda m: "!!" if rng.random() < 0.45 else m.group(0), out)
    out = re.sub(r"\?{3,}", lambda m: "??" if rng.random() < 0.45 else m.group(0), out)
    return out


def add_repeated_letter(text: str, rng: random.Random, rate: float = 0.012) -> str:
    if not text or rng.random() >= rate:
        return text
    parts = re.split(r"(\s+)", text)
    eligible = [i for i, p in enumerate(parts) if re.fullmatch(r"[A-Za-z]{3,8}", p)]
    if not eligible:
        return text
    i = rng.choice(eligible)
    pos = rng.randint(1, len(parts[i]) - 1)
    parts[i] = parts[i][:pos] + parts[i][pos] + parts[i][pos:]
    return "".join(parts)


def humanize(text: str, rng: random.Random | None = None, *, abbreviation_rate: float | None = None,
             typo_rate: float | None = None, case_rate: float | None = None,
             space_rate: float | None = None) -> str:
    rng = rng or random
    out = str(text or "").strip()
    if not out:
        return out
    out = apply_abbreviations(out, rng, config.ABBREV_RATE if abbreviation_rate is None else abbreviation_rate)
    out = add_typo(out, rng, config.TYPO_RATE if typo_rate is None else typo_rate)
    out = add_case_variation(out, rng, config.CASE_RATE if case_rate is None else case_rate)
    out = add_space_slip(out, rng, config.SPACE_RATE if space_rate is None else space_rate)
    out = add_casual_punctuation(out, rng)
    out = add_repeated_letter(out, rng)
    return out


# =============================================================================
# 2. TIMING
# =============================================================================
@dataclass
class HumanContext:
    thread_id: str = "unknown"
    incoming_text: str = ""
    incoming_words: int = 0
    recent_messages: int = 1
    momentum: int = 1
    mode: str = "chill"  # chill | normal | rushed


class ConversationTracker:
    """How busy is this GC right now? Busy chats get quicker, shorter-fused replies."""

    WINDOW_S = 45

    def __init__(self):
        self._state: dict[str, tuple[float, int]] = {}

    def context(self, thread_id, incoming_text: str = "") -> HumanContext:
        key, now = str(thread_id or "unknown"), time.monotonic()
        prev = self._state.get(key)
        recent = min(8, prev[1] + 1) if prev and now - prev[0] < self.WINDOW_S else 1
        self._state[key] = (now, recent)
        if len(self._state) > 2000:
            self._state = {k: v for k, v in self._state.items() if now - v[0] < self.WINDOW_S}
        words = len(str(incoming_text).split())
        mode = "rushed" if recent >= 5 else "normal" if recent >= 3 else "chill"
        momentum = max(1, recent + (words >= 18) + (words >= 35))
        return HumanContext(key, str(incoming_text), words, recent, momentum, mode)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _word_complexity(word: str) -> float:
    clean = re.sub(r"[^A-Za-z0-9']", "", word)
    if not clean:
        return 0.0
    score = 1.0 if len(clean) >= 7 else 0.0
    if re.search(r"[^aeiou]{4,}", clean, re.I):
        score += 0.5
    if re.search(r"\d", clean):
        score += 0.25
    return score


def reaction_delay_ms(text: str, ctx: HumanContext, rng: random.Random, hesitation_rate: float, max_ms: float) -> int:
    """Time before the 'typing...' bubble appears (reading + thinking)."""
    value = text.strip()
    words = len(value.split())
    delay = 220 + min(900, words * 35)
    delay += 120 if ctx.mode == "chill" else -90 if ctx.mode == "rushed" else 0
    delay += 90 * (ctx.incoming_words >= 18) + 60 * (ctx.incoming_words >= 35)
    delay += 40 * (ctx.recent_messages >= 3) - 70 * (ctx.recent_messages >= 5)
    if len(value) > 12 and rng.random() < hesitation_rate:  # occasional "hmm..." pause
        delay += rng.uniform(120, 600)
    delay -= min(120, ctx.momentum * 12)
    delay += (max(1, len(re.split(r"[.!?]+", value))) - 1) * rng.uniform(25, 100)
    delay += rng.uniform(-130, 130)
    return int(_clamp(delay, 120, max_ms))


def typing_ms(text: str, ctx: HumanContext, rng: random.Random, max_ms: float,
              min_cps: float = 5.5, max_cps: float = 10.5) -> int:
    """How long the 'typing...' bubble stays: ~5-10 chars/sec plus pauses at punctuation."""
    value = text.strip()
    if not value:
        return 350
    words = value.split()
    complexity = sum(_word_complexity(w) for w in words) / len(words)
    delay = len(value) / rng.uniform(min_cps, max_cps) * 1000
    delay += complexity * len(words) * 55
    delay += value.count(",") * 87 + len(re.findall(r"[;:]", value)) * 113
    delay += len(re.findall(r"[!?]", value)) * 157 + value.count("...") * 270 + value.count(".") * 55
    delay += min(6, len(re.findall(r"[\U0001F300-\U0001FAFF]", value))) * 12
    delay *= 1.12 if ctx.mode == "chill" else 0.78 if ctx.mode == "rushed" else 1
    delay *= 0.88 if ctx.momentum >= 4 else 0.94 if ctx.momentum >= 3 else 1
    spread = min(900, 180 + delay * 0.28)
    delay += rng.uniform(-spread, spread)
    if len(words) >= 4 and rng.random() < 0.24:  # small mid-sentence pause
        delay += rng.choice([80, 110, 140, 180, 220, 260, 320])
    return int(_clamp(delay, 350, max_ms))


# --- social "how should this reply feel" layer (only affects speed) ---------
_REACTION_WORDS = {"lol", "lmao", "lmfao", "haha", "hahaha", "fr", "real", "damn", "wtf", "bro", "bruh", "nah", "😭", "💀", "🤣"}
SOCIAL_SPEED = {"reaction": 0.55, "react": 0.75, "short": 0.72, "answer": 1.05, "ask": 1.02,
                "callback": 1.08, "deflect": 0.82, "agree": 0.78, "tease": 1.0}


def classify_social(incoming: str, candidate: str, ctx: HumanContext) -> str:
    text = re.sub(r"\s+", " ", re.sub(r"https?://\S+", "", incoming.lower())).strip()
    words = text.split()
    energy = sum([
        bool(re.search(r"[!?]{2,}", incoming)),
        bool(re.search(r"\b(?:haha+|lmao+|lmfao+)\b", text)),
        bool(re.search(r"[😭💀🤣]", incoming)),
        bool(re.search(r"\b(?:bro|bruh|wtf|nah)\b", text)),
        incoming.isupper() and bool(re.search(r"[A-Z]", incoming)),
    ])
    if 0 < len(words) <= 3 and (all(w in _REACTION_WORDS for w in words) or re.fullmatch(r"[!?😭💀🤣.]+", text)):
        return "reaction"
    if "?" in text or re.search(r"\b(?:why|what|how|who|where|when|bakit|ano|paano|sino|saan|kelan)\b", text):
        return "answer"
    if ctx.momentum >= 4 and len(words) <= 8:
        return "short"
    if energy >= 3:
        return "react"
    if re.search(r"\b(?:agree|tama|true|real|same|exactly|oo|yes|yep|yup)\b", text):
        return "agree"
    if re.search(r"\b(?:idk|i don't know|di ko alam|wala|nothing|nvm|never mind)\b", text):
        return "deflect"
    if candidate.rstrip().endswith("?"):
        return "ask"
    if ctx.recent_messages >= 3 and len(words) >= 12:
        return "callback"
    return "tease"


@dataclass
class Prepared:
    text: str
    reaction_ms: int
    typing_ms: int
    social: str


# =============================================================================
# 3. TYPING INDICATOR + SENDER
# =============================================================================
class TypingIndicator:
    """Shows 'typing...' for `duration_s`, re-sending it because Messenger drops it after a few seconds."""

    def __init__(self, set_typing: Callable[[str, bool], Awaitable[None]], refresh_s: float | None = None):
        self._set_typing = set_typing
        self.refresh_s = refresh_s or config.TYPING_REFRESH_S

    async def _safe(self, thread_id: str, on: bool) -> None:
        try:
            await self._set_typing(thread_id, on)
        except Exception as exc:  # a failed typing bubble must never block the reply
            log.debug("typing indicator failed: %s", exc)

    async def hold(self, thread_id: str, duration_s: float) -> None:
        end = time.monotonic() + max(0.0, duration_s)
        await self._safe(thread_id, True)
        try:
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    break
                await asyncio.sleep(min(self.refresh_s, left))
                if end - time.monotonic() > 0:
                    await self._safe(thread_id, True)
        finally:
            await self._safe(thread_id, False)


class HumanSender:
    def __init__(self, set_typing: Callable[[str, bool], Awaitable[None]], rng: random.Random | None = None):
        self.rng = rng or random.Random()
        self.tracker = ConversationTracker()
        self.typing = TypingIndicator(set_typing)

    def prepare(self, thread_id, reply: str, incoming_text: str = "") -> Prepared:
        ctx = self.tracker.context(thread_id, incoming_text)
        text = humanize(reply, self.rng)
        social = classify_social(incoming_text, text, ctx)
        mult = SOCIAL_SPEED.get(social, 1.0) * config.SPEED
        max_ms = config.MAX_DELAY_MS
        react = reaction_delay_ms(text, ctx, self.rng, config.HESITATION_RATE, max_ms) * mult
        typed = typing_ms(text, ctx, self.rng, max_ms) * mult
        return Prepared(text, int(_clamp(react, 120, 900)), int(_clamp(typed, 500, max_ms)), social)

    async def deliver(self, thread_id, prepared: Prepared, send: Callable[[str], Awaitable]):
        """pause -> typing bubble -> send(text)"""
        await asyncio.sleep(prepared.reaction_ms / 1000)
        await self.typing.hold(str(thread_id), prepared.typing_ms / 1000)
        return await send(prepared.text)
