"""Trigger -> reply logic.

Order of matching for a message in a target GC:
  1. dynamic rules  (data/dynamic_rules.json)  e.g. "asoka"  -> "mamamo aso"
                    regex + templates, e.g. "tanga ka" -> "{w} ka rin" (echoes the word)
  2. reply groups   (data/reply_groups.json)   keyword lists with big reply pools
  3. fallback       (optional)                 any line from all pools, if the caller allows it

Every pool is served through the per-thread shuffle bag in memory.py, so the same GC never
gets the same line twice until the whole pool has been used.
"""
from __future__ import annotations

import json
import logging
import random
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import config
from memory import ReplyMemory

log = logging.getLogger("triggers")


@dataclass
class Reply:
    text: str
    source: str  # dynamic | group | fallback | command
    rule: str


@dataclass
class _Rule:
    name: str
    pattern: re.Pattern
    replies: list[str]


@dataclass
class _Group:
    name: str
    triggers: list[str]
    replies: list[str]
    _short: list[re.Pattern]
    _long: list[str]


def normalize(text: str) -> str:
    """Lowercase, collapse spaces and squeeze stretched letters ('asokaaaa' -> 'asoka')."""
    value = re.sub(r"\s+", " ", str(text or "").lower()).strip()
    return re.sub(r"(.)\1{2,}", r"\1", value)


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf8"))
    except FileNotFoundError:
        return default
    except Exception as exc:  # a typo in a JSON file must not kill the bot
        log.error("could not read %s: %s", path, exc)
        return default


class _SafeDict(defaultdict):
    def __missing__(self, key):
        return ""


class TriggerEngine:
    def __init__(self, memory: ReplyMemory | None = None, rng: random.Random | None = None, data_dir: Path | None = None):
        self.memory = memory or ReplyMemory()
        self.rng = rng or random.Random()
        self.data_dir = data_dir or config.ASSETS_DIR
        self.rules: list[_Rule] = []
        self.groups: list[_Group] = []
        self.command_replies: dict[str, list[str]] = {}
        self.reload()

    # ------------------------------------------------------------------ loading
    def reload(self) -> None:
        self.rules = []
        for item in _load_json(self.data_dir / "dynamic_rules.json", []):
            try:
                self.rules.append(_Rule(item["name"], re.compile(item["pattern"], re.I), list(item["replies"])))
            except Exception as exc:
                log.error("bad dynamic rule %r: %s", item.get("name"), exc)

        self.groups = []
        for item in _load_json(self.data_dir / "reply_groups.json", []):
            trig = [str(t).lower() for t in item.get("triggers", []) if str(t).strip()]
            self.groups.append(_Group(
                name=item["name"], triggers=trig, replies=list(item.get("replies", [])),
                # short triggers ("bot", "ai") must match as whole words, longer ones as substrings
                _short=[re.compile(rf"\b{re.escape(t)}\b", re.I) for t in trig if len(t) <= 4],
                _long=[t for t in trig if len(t) > 4],
            ))
        self.command_replies = _load_json(self.data_dir / "command_replies.json", {})
        log.info("loaded %d dynamic rules, %d reply groups", len(self.rules), len(self.groups))

    # ------------------------------------------------------------------ matching
    def _match_rule(self, text: str):
        for rule in self.rules:
            m = rule.pattern.search(text)
            if m:
                return rule, m
        return None, None

    def _match_groups(self, text: str) -> list[_Group]:
        return [g for g in self.groups if any(p.search(text) for p in g._short) or any(t in text for t in g._long)]

    def matches(self, text: str) -> bool:
        text = normalize(text)
        return self._match_rule(text)[0] is not None or bool(self._match_groups(text))

    # ------------------------------------------------------------------ public API
    def reply_for(self, thread_id, body: str, fallback: bool = False) -> Reply | None:
        """Return a reply for `body`, or None if nothing matched (and fallback is False)."""
        text = normalize(body)
        if not text:
            return None

        rule, m = self._match_rule(text)
        if rule:
            template = self.memory.pick(thread_id, f"dyn:{rule.name}", rule.replies)
            if template:
                words = _SafeDict(str, {k: (v or "") for k, v in m.groupdict().items()})
                try:
                    return Reply(template.format_map(words), "dynamic", rule.name)
                except (IndexError, ValueError):
                    return Reply(template, "dynamic", rule.name)

        matched = self._match_groups(text)
        if matched:
            group = self.rng.choice(matched)
            pick = self.memory.pick(thread_id, f"grp:{group.name}", group.replies)
            if pick:
                return Reply(pick, "group", group.name)

        if fallback:
            pool = [r for g in self.groups for r in g.replies]
            pick = self.memory.pick(thread_id, "fallback", pool)
            if pick:
                return Reply(pick, "fallback", "all")
        return None

    def command_reply(self, kind: str, thread_id) -> str | None:
        """Lines for !banat on / off and for people who are not allowed to use commands."""
        return self.memory.pick(thread_id, f"cmd:{kind}", self.command_replies.get(kind, []), remember=False)
