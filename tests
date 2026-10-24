"""Offline tests (no Facebook, no network). Run from the project folder:

    python -m unittest discover -s tests -v
"""
import asyncio
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

# ---- test configuration must be set BEFORE `config` is imported ----------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP = tempfile.mkdtemp(prefix="banat-test-")
os.environ.update({
    "DATA_DIR": _TMP, "BANAT_SPEED": "0.2", "BANAT_THREAD_COOLDOWN_S": "0", "BANAT_FALLBACK_CHANCE": "1",
    "BANAT_SHORTHAND_RATE": "0", "BANAT_TYPO_RATE": "0", "BANAT_CASE_VARIATION_RATE": "0",
    "BANAT_SPACE_SLIP_RATE": "0", "DASHBOARD_API_KEY": "test-key", "BANAT_TARGETS": "",
    "BANAT_PROBE_INTERVAL_S": "30", "BANAT_MAX_PENDING": "2",
})
os.environ.pop("FB_COOKIES", None)

import config  # noqa: E402
import humanizer  # noqa: E402
from main import BotEngine  # noqa: E402
from memory import ReplyMemory, ShuffleBag  # noqa: E402
from messenger import AuthError, FakeClient, IncomingMessage  # noqa: E402
from store import SessionError, Store, parse_cookies  # noqa: E402
from triggers import TriggerEngine  # noqa: E402

GC = "1234567890123"
ME = "100000000000001"
OTHER = "100000000000002"


class ShuffleBagTests(unittest.TestCase):
    def test_every_item_once_per_rotation(self):
        bag, pool = ShuffleBag(random.Random(1)), [f"line {i}" for i in range(10)]
        first = [bag.pick("t", "p", pool) for _ in range(10)]
        self.assertEqual(sorted(first), sorted(pool))
        second = [bag.pick("t", "p", pool) for _ in range(10)]
        self.assertEqual(sorted(second), sorted(pool))
        self.assertNotEqual(first[-1], second[0], "rotation boundary repeat")

    def test_threads_are_independent(self):
        bag, pool = ShuffleBag(random.Random(2)), [f"x{i}" for i in range(5)]
        a = {bag.pick("A", "p", pool) for _ in range(5)}
        b = {bag.pick("B", "p", pool) for _ in range(5)}
        self.assertEqual(a, set(pool))
        self.assertEqual(b, set(pool))

    def test_pool_smaller_than_memory_window_never_repeats_back_to_back(self):
        mem = ReplyMemory(ShuffleBag(random.Random(3)))
        pool = [f"reply number {i} uniq{i}" for i in range(17)]
        picks = [mem.pick(GC, "grp", pool) for _ in range(60)]
        self.assertTrue(all(p is not None for p in picks))
        self.assertTrue(all(a != b for a, b in zip(picks, picks[1:])))
        self.assertEqual(len(set(picks[:17])), 17)

    def test_empty_pool(self):
        self.assertIsNone(ShuffleBag().pick("t", "p", []))


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.t = TriggerEngine(ReplyMemory(ShuffleBag(random.Random(4))), random.Random(4))

    def test_asoka_dynamic(self):
        r = self.t.reply_for(GC, "ASOKA!!!")
        self.assertEqual((r.source, r.rule), ("dynamic", "asoka"))
        self.assertIn("aso", r.text.lower())

    def test_echo_template(self):
        r = self.t.reply_for(GC, "tanga ka")
        self.assertEqual(r.rule, "echo-insult")
        self.assertIn("tanga", r.text)
        self.assertNotIn("{", r.text)

    def test_group_and_fallback(self):
        self.assertEqual(self.t.reply_for(GC, "ang bobo mo").source in ("dynamic", "group"), True)
        self.assertIsNone(self.t.reply_for(GC, "kumain na ako ng tanghalian"))
        self.assertEqual(self.t.reply_for(GC, "kumain na ako ng tanghalian", fallback=True).source, "fallback")

    def test_short_trigger_needs_whole_word(self):
        self.assertFalse(self.t.matches("ang bait ng aircon"))   # 'ai' inside a word must not fire
        self.assertTrue(self.t.matches("bot ka"))

    def test_no_repeat_in_one_gc(self):
        seen = [self.t.reply_for(GC, "wala kang kwenta, weak").text for _ in range(40)]
        self.assertEqual(len(set(seen)), 40)

    def test_command_pools(self):
        for kind in ("on", "off", "unauthorized"):
            self.assertTrue(self.t.command_reply(kind, GC))


class HumanizerTests(unittest.TestCase):
    def test_shorthand(self):
        rng = random.Random(5)
        out = {humanizer.apply_abbreviations("bakit hindi ka sumasagot", rng, 1.0) for _ in range(40)}
        self.assertTrue(any("bkt" in o or "di ka" in o for o in out))
        self.assertTrue(all(o != "bakit hindi ka sumasagot" for o in out))

    def test_case_is_preserved(self):
        self.assertEqual(humanizer.apply_abbreviations("Bakit", random.Random(1), 1.0), "Bkt")

    def test_typo_changes_at_most_one_word(self):
        text = "ang kapal ng mukha mo parang tinapay"
        for seed in range(30):
            out = humanizer.add_typo(text, random.Random(seed), 1.0)
            diff = sum(a != b for a, b in zip(out.split(), text.split())) + abs(len(out.split()) - len(text.split()))
            self.assertLessEqual(diff, 1)

    def test_timing_scales_with_length_and_stays_in_bounds(self):
        hs = humanizer.HumanSender(lambda t, on: asyncio.sleep(0), random.Random(6))
        short = [hs.prepare(f"s{i}", "ok", "hi") for i in range(40)]
        long_ = [hs.prepare(f"l{i}", "ang haba ng speech mo, nasaan ang point, may point ka ba o nagbabakasyon lang?", "hi") for i in range(40)]
        avg = lambda xs: sum(x.typing_ms for x in xs) / len(xs)
        self.assertLess(avg(short), avg(long_))
        for p in short + long_:
            self.assertTrue(120 <= p.reaction_ms <= 900)
            self.assertTrue(500 <= p.typing_ms <= config.MAX_DELAY_MS)


class StoreTests(unittest.TestCase):
    def test_cookie_formats(self):
        self.assertEqual({c["name"] for c in parse_cookies("c_user=1; xs=2; datr=3")}, {"c_user", "xs", "datr"})
        self.assertTrue(parse_cookies('[{"key":"c_user","value":"1"},{"name":"xs","value":"2"}]'))
        self.assertTrue(parse_cookies('{"cookies":[{"name":"c_user","value":"1"},{"name":"xs","value":"2"}]}'))
        for bad in ("", "hello", "c_user=1", "[]", "{}"):
            with self.assertRaises(SessionError):
                parse_cookies(bad)

    def test_targets_and_session_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d))
            s.add_target(GC)
            with self.assertRaises(ValueError):
                s.add_target("abc")
            s.save_session("c_user=42; xs=abc")
            self.assertEqual(oct(os.stat(s.cookie_file).st_mode & 0o777), "0o600")
            s2 = Store(Path(d))  # "restart"
            self.assertTrue(s2.is_target(GC))
            self.assertEqual(s2.session_info()["account"], "c_user 42")
            self.assertNotIn("abc", str(s2.session_info()))  # cookie value never leaves the store


async def until(cond, timeout=8.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.dir.name))
        self.store.save_session(f"c_user={ME}; xs=abc")
        self.clients: list[FakeClient] = []

        def factory():
            c = FakeClient(uid=ME)
            if getattr(self, "login_error", None):
                c.fail_login = self.login_error
            self.clients.append(c)
            return c

        self.engine = BotEngine(self.store, factory, random.Random(7))
        self.engine.start()

    async def asyncTearDown(self):
        await self.engine.shutdown()
        self.dir.cleanup()

    @property
    def fake(self) -> FakeClient:
        return self.clients[-1]

    def say(self, text, thread=GC, sender=OTHER, mid="m1"):
        self.fake.inject(IncomingMessage(thread, sender, text, mid))

    async def test_only_target_gcs_are_answered(self):
        self.assertTrue(await until(lambda: self.engine.connected))
        self.say("asoka")                       # GC not activated yet
        await asyncio.sleep(1.5)
        self.assertEqual(self.fake.sent, [])

        await self.engine.add_target(GC)
        self.say("asoka")
        self.assertTrue(await until(lambda: len(self.fake.sent) == 1))
        thread, text, reply_to = self.fake.sent[0]
        self.assertEqual((thread, reply_to), (GC, "m1"))
        self.assertIn("aso", text.lower())
        self.assertEqual(self.fake.typing_calls[0], (GC, True))   # typing bubble shown before sending
        self.assertEqual(self.fake.typing_calls[-1], (GC, False)) # and cleared after

        self.say("hello", thread="999999999", sender=OTHER)       # other GC stays silent
        self.say("asoka", sender=ME)                              # never answers itself
        await asyncio.sleep(1.5)
        self.assertEqual(len(self.fake.sent), 1)

    async def test_commands_owner_only(self):
        self.assertTrue(await until(lambda: self.engine.connected))
        self.say("!banat on", sender=OTHER, thread="777777777")   # stranger, GC not active -> ignored silently
        await asyncio.sleep(0.5)
        self.assertFalse(self.store.is_target("777777777"))
        self.assertEqual(self.fake.sent, [])

        self.say("!banat on", sender=ME, thread="777777777")      # owner activates
        self.assertTrue(await until(lambda: self.store.is_target("777777777")))
        self.assertTrue(await until(lambda: len(self.fake.sent) == 1))

        self.say("!banat off", sender=OTHER, thread="777777777")  # stranger in active GC -> mocked, still ON
        self.assertTrue(await until(lambda: len(self.fake.sent) == 2))
        self.assertTrue(self.store.is_target("777777777"))

        self.say("!banat off", sender=ME, thread="777777777")
        self.assertTrue(await until(lambda: not self.store.is_target("777777777")))

    async def test_busy_gc_skips_instead_of_piling_up(self):
        self.assertTrue(await until(lambda: self.engine.connected))
        await self.engine.add_target(GC)
        for i in range(8):
            self.say("asoka", mid=f"m{i}")
        await asyncio.sleep(0.5)
        self.assertGreaterEqual(self.engine.stats["skipped"], 5)

    async def test_auto_reconnect_after_drop(self):
        self.assertTrue(await until(lambda: self.engine.connected))
        first = self.fake
        first.drop()
        self.assertTrue(await until(lambda: not self.engine.connected, 3))
        self.assertTrue(await until(lambda: self.engine.connected and self.fake is not first, 10), "did not reconnect")

    async def test_dead_cookie_is_not_retried(self):
        await self.engine.shutdown()
        self.login_error = AuthError("checkpoint required")
        self.clients.clear()
        self.engine = BotEngine(self.store, self.engine.client_factory, random.Random(8))
        self.engine.start()
        self.assertTrue(await until(lambda: self.engine.needs_cookie))
        await asyncio.sleep(1)
        self.assertEqual(len(self.clients), 1, "must not hammer login with a dead cookie")
        self.login_error = None
        self.engine.connect_session(f"c_user={ME}; xs=fresh")       # new cookie -> tries again
        self.assertTrue(await until(lambda: self.engine.connected))

    async def test_stop_and_start(self):
        self.assertTrue(await until(lambda: self.engine.connected))
        self.engine.stop_bot()
        self.assertTrue(await until(lambda: not self.engine.connected))
        await asyncio.sleep(0.5)
        self.assertFalse(self.engine.connected)
        self.engine.start_bot()
        self.assertTrue(await until(lambda: self.engine.connected))

    async def test_gc_cycle(self):
        self.assertTrue(await until(lambda: self.engine.connected))
        with self.assertRaises(ValueError):                         # not a target GC
            await self.engine.cycle.start(GC, lines=["a"])
        await self.engine.add_target(GC)
        info = await self.engine.cycle.start(GC, lines=["roast one", "roast two"], interval_s=2.5)
        self.assertEqual(info["members"], 3)
        self.assertTrue(await until(lambda: len(self.fake.nicks) >= 3, 12))
        self.assertEqual(self.fake.names[0], (GC, "roast one"))
        self.assertEqual({n[1] for n in self.fake.nicks[:3]}, set(self.fake.participants))
        self.assertTrue(await self.engine.cycle.stop(GC))
        self.assertEqual(self.engine.cycle.status(), [])


if __name__ == "__main__":
    unittest.main()
