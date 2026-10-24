# Banat Bot (Python)

Messenger auto-reply bot para sa mga **naka-target na GC lang**, may web dashboard, auto-reconnect,
at GC cycle (title + nicknames). Isang process lang ang tumatakbo (`python main.py`): bot + dashboard.

```
main.py          engine: login -> listen -> auto-reconnect, GC-only replies, chat commands, entry point
dashboard.py     FastAPI: /health, UI (cookie, start/stop, target GCs, cycle), /api/*
triggers.py      trigger -> reply (dynamic rules, reply groups, fallback)
humanizer.py     shorthand (bakit -> bkt), typos, typing delay + typing indicator
memory.py        no-repeat shuffle bag + per-GC memory
messenger.py     adapter sa Messenger library (fbchat-muqit) + FakeClient pang-test
gc_cycle.py      auto-change ng GC title at nicknames galing sa roasts.txt
traffic.py       send limits / cooldown / retry
store.py         JSON storage: targets, settings, cookie
config.py        lahat ng env settings
data/            reply_groups.json, dynamic_rules.json, command_replies.json, roasts.txt
tests/           offline tests  ->  python -m unittest discover -s tests -v
```

## Takbo sa local
```bash
pip install -r requirements.txt
DASHBOARD_API_KEY=password-mo python main.py      # buksan http://localhost:8000
```
Walang Facebook? `MESSENGER_BACKEND=fake python main.py` para subukan lang ang dashboard.

## Deploy sa Railway
1. I-push ang folder sa GitHub (**private repo**), New Project -> Deploy from GitHub.
2. Variables: `DASHBOARD_API_KEY` (mahabang password), `DATA_DIR=/data`.
3. Add **Volume**, mount path `/data` (para hindi mawala ang cookie at target GCs tuwing redeploy).
4. Settings -> Networking -> Generate Domain. Healthcheck ay `/health` (nakaset na sa `railway.json`).
5. Buksan ang domain, ilagay ang `DASHBOARD_API_KEY`, i-paste ang cookie, idagdag ang Thread ID ng GC.

## Paano dagdagan ang mga banat
* `data/dynamic_rules.json` - regex + reply. Gumagamit ng `{w}` para ulitin ang salita ng kalaban
  (`"tanga ka"` -> `"tanga ka rin"`). Halimbawa: `asoka` -> `mamamo aso`.
* `data/reply_groups.json` - mga keyword at malalaking reply pool.
* `data/roasts.txt` - isang line = isang step ng GC cycle (ang `#` lines ay hindi binibilang).

## Commands sa chat (owner lang: yung bot account mismo o `BANAT_OWNER_IDS`)
`!banat on|off|toggle|status`, `!cycle on [seconds]|off|status`.
Ibang tao na gagamit ng command sa activated na GC ay aasarin; sa hindi activated na GC, hindi papansinin.

## Mahalaga
* Ang `fbchat-muqit` code sa `messenger.py` ay isinulat batay sa documentation nila at hindi pa nasubukan
  laban sa totoong Facebook. Kapag may nag-error sa login/send/typing, doon lang sa `MuqitClient` class
  ang aayusin; ang buong ibang bahagi ay hindi maaapektuhan.
* Kapag tinanggihan ng Facebook ang cookie (checkpoint/logout), hihinto ang bot sa pagre-retry at maghihintay
  ng bagong cookie. Makikita ito sa dashboard.
* Ang unofficial na pag-automate ng Facebook account ay labag sa Terms nila at puwedeng magresulta sa
  checkpoint o ban. Gumamit ng hiwalay na account, at huwag masyadong bilisan ang GC cycle.
