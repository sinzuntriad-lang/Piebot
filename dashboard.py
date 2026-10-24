"""Web dashboard + API (FastAPI, served by uvicorn from main.py).

Public   : GET /health            (Railway healthcheck - always 200 while the web server is up)
           GET /                  (the UI; it asks for the dashboard key)
Protected: /api/*                 header  X-Dashboard-Key: <DASHBOARD_API_KEY>   (or Authorization: Bearer ...)

NOTE: no `from __future__ import annotations` here on purpose - FastAPI/pydantic resolve the
type hints of the request models at runtime.
"""
import hmac
import re
import time
from typing import List, Optional, Union

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import config
from gc_cycle import load_lines
from store import SessionError

# --------------------------------------------------------------------------- request bodies
class CookieIn(BaseModel):
    cookie: str


class ThreadsIn(BaseModel):
    thread_ids: Union[str, List[str]]  # "123, 456\n789" or ["123", "456"]


class SettingsIn(BaseModel):
    fallback_chance: Optional[float] = Field(None, ge=0, le=1)


class CycleStartIn(BaseModel):
    thread_id: str
    lines: Optional[List[str]] = None  # empty -> use roasts.txt
    do_name: bool = True
    do_nick: bool = True
    interval_seconds: float = 3.0


class CycleStopIn(BaseModel):
    thread_id: Optional[str] = None  # empty -> stop all


def split_ids(value) -> List[str]:
    if isinstance(value, str):
        value = re.split(r"[\s,;]+", value)
    return [str(v).strip() for v in value if str(v).strip()]


# --------------------------------------------------------------------------- auth (with a tiny brute-force limiter)
_FAILS = {}  # ip -> [timestamps]


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def require_key(request: Request) -> None:
    ip, now = _client_ip(request), time.time()
    recent = [t for t in _FAILS.get(ip, []) if now - t < 60]
    if len(recent) >= 10:
        raise HTTPException(429, "Too many wrong keys. Try again in a minute.")

    supplied = request.headers.get("x-dashboard-key") or re.sub(
        r"^Bearer\s+", "", request.headers.get("authorization", ""), flags=re.I)
    if not hmac.compare_digest(supplied.strip().encode(), config.DASHBOARD_KEY.encode()):
        _FAILS[ip] = recent + [now]
        raise HTTPException(401, "Dashboard authorization required")
    _FAILS.pop(ip, None)


# --------------------------------------------------------------------------- app factory
def create_app(engine) -> FastAPI:
    app = FastAPI(title="Banat Bot", docs_url=None, redoc_url=None, openapi_url=None)
    api = APIRouter(prefix="/api", dependencies=[Depends(require_key)])

    def bad_request(exc: Exception) -> HTTPException:
        return HTTPException(400, str(exc))

    # ---- public -------------------------------------------------------------
    @app.get("/health")
    async def health():
        # Must stay 200 even when Messenger is down, or Railway would kill the deploy.
        return {
            "ok": True,
            "service": "banat-bot",
            "messenger_connected": engine.connected,
            "uptime_seconds": int(time.monotonic() - engine.started_at),
        }

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(PAGE, headers={"Cache-Control": "no-store"})

    # ---- status ----------------------------------------------------------------
    @api.get("/status")
    async def status():
        data = engine.status()
        data["session"] = engine.store.session_info()
        data["targets"] = engine.store.list_targets()
        return data

    @api.get("/events")
    async def events():
        return {"ok": True, "events": list(engine.events)[:80]}

    # ---- bot control -----------------------------------------------------------
    @api.post("/bot/start")
    async def bot_start():
        engine.start_bot()
        return {"ok": True}

    @api.post("/bot/stop")
    async def bot_stop():
        engine.stop_bot()
        return {"ok": True}

    @api.post("/bot/reconnect")
    async def bot_reconnect():
        engine.reconnect()
        return {"ok": True}

    # ---- facebook session --------------------------------------------------------
    @api.post("/session")
    async def session_save(body: CookieIn):
        try:
            return {"ok": True, "session": engine.connect_session(body.cookie)}
        except SessionError as exc:
            raise bad_request(exc)

    @api.delete("/session")
    async def session_delete():
        engine.clear_session()
        return {"ok": True}

    # ---- target GCs ------------------------------------------------------------------
    @api.get("/targets")
    async def targets_get():
        return {"ok": True, "targets": engine.store.list_targets()}

    @api.put("/targets")
    async def targets_replace(body: ThreadsIn):
        try:
            await engine.set_targets(split_ids(body.thread_ids))
        except ValueError as exc:
            raise bad_request(exc)
        return {"ok": True, "targets": engine.store.list_targets()}

    @api.post("/targets")
    async def targets_add(body: ThreadsIn):
        try:
            for tid in split_ids(body.thread_ids):
                await engine.add_target(tid)
        except ValueError as exc:
            raise bad_request(exc)
        return {"ok": True, "targets": engine.store.list_targets()}

    @api.delete("/targets/{thread_id}")
    async def targets_remove(thread_id: str):
        await engine.remove_target(thread_id)
        return {"ok": True, "targets": engine.store.list_targets()}

    # ---- settings ---------------------------------------------------------------------
    @api.put("/settings")
    async def settings_put(body: SettingsIn):
        return {"ok": True, "settings": engine.store.update_settings(fallback_chance=body.fallback_chance)}

    # ---- GC cycle -----------------------------------------------------------------------
    @api.get("/cycle")
    async def cycle_get():
        try:
            lines = len(load_lines())
        except Exception as exc:
            lines = 0
            note = str(exc)
        else:
            note = None
        return {"ok": True, "cycles": engine.cycle.status(), "roasts_lines": lines, "roasts_note": note}

    @api.post("/cycle/start")
    async def cycle_start(body: CycleStartIn):
        try:
            info = await engine.cycle.start(
                body.thread_id.strip(), lines=body.lines, do_name=body.do_name,
                do_nick=body.do_nick, interval_s=body.interval_seconds)
        except (ValueError, FileNotFoundError) as exc:
            raise bad_request(exc)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"ok": True, "cycle": info}

    @api.post("/cycle/stop")
    async def cycle_stop(body: CycleStopIn):
        if body.thread_id:
            await engine.cycle.stop(body.thread_id.strip())
        else:
            await engine.cycle.stop_all()
        return {"ok": True}

    app.include_router(api)
    return app


# --------------------------------------------------------------------------- the UI
PAGE = r"""<!doctype html>
<html lang="tl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Banat Bot</title>
<style>
:root{--bg:#0b0a14;--card:#13111f;--line:#2a2646;--tx:#eceaff;--mut:#8d89b6;--ac:#8b7bff;--ok:#38e0a0;--bad:#ff5a7a;--warn:#ffc857}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--tx);background:var(--bg)}
header{display:flex;align-items:center;gap:12px;padding:14px 16px;border-bottom:1px solid var(--line)}
header h1{margin:0;font-size:16px;letter-spacing:.28em}
header small{color:var(--mut);display:block;font-size:10px;letter-spacing:.2em;margin-top:2px}
main{max-width:760px;margin:0 auto;padding:14px 16px 40px;display:grid;gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px}
h2{margin:0 0 10px;font-size:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.k{font-size:10px;letter-spacing:.16em;color:var(--mut);text-transform:uppercase}
.v{font-size:17px;font-weight:700;margin-top:4px;word-break:break-word}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
.pill{margin-left:auto;padding:5px 12px;border-radius:99px;font-size:12px;font-weight:700;background:#ff5a7a22;color:var(--bad)}
.pill.on{background:#38e0a022;color:var(--ok)}
textarea,input[type=text],input[type=password],input[type=number],select{width:100%;background:#0a0916;border:1px solid var(--line);color:var(--tx);border-radius:10px;padding:10px;font:13px ui-monospace,Menlo,monospace}
textarea{min-height:90px;resize:vertical}
button{border:0;border-radius:10px;padding:10px 14px;font-weight:700;color:#fff;background:var(--ac);cursor:pointer;font-size:13px}
button.g{background:#1d1a35;border:1px solid var(--line)}button.r{background:#a8233f}button.s{background:#16825a}
button:disabled{opacity:.5;cursor:default}
.row{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}.row>*{flex:1;min-width:110px}
.kv{display:flex;justify-content:space-between;gap:10px;padding:7px 0;border-bottom:1px solid var(--line);font-size:13px;align-items:center}
.kv>span:first-child{color:var(--mut)}.kv>span:last-child{text-align:right;word-break:break-all}
.ev{font:12px ui-monospace,Menlo,monospace;padding:6px 0;border-bottom:1px solid var(--line);color:var(--mut);word-break:break-word}.ev b{color:var(--tx)}
#err{display:none;background:#ff5a7a1c;border:1px solid #ff5a7a66;color:#ffb3c1;border-radius:12px;padding:10px;font-size:13px}
#warn{display:none;background:#ffc8571c;border:1px solid #ffc85766;color:#ffe2a3;border-radius:12px;padding:10px;font-size:13px}
label.chk{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--mut)}
.hint{font-size:12px;color:var(--mut);margin:6px 0 0}
#login{display:none}#app{display:none;gap:12px}
</style></head><body>
<header><div><h1>BANAT BOT</h1><small>MESSENGER CONTROL</small></div><span id="conn" class="pill">Disconnected</span></header>
<main>
<div id="err"></div><div id="warn"></div>

<div class="card" id="login"><h2>Dashboard key</h2>
<input id="key" type="password" placeholder="DASHBOARD_API_KEY" autocomplete="off">
<div class="row"><button id="unlock">Unlock</button></div>
<p class="hint">Ang key ay yung <b>DASHBOARD_API_KEY</b> sa Railway Variables (o nasa deploy logs kung hindi mo pa nase-set).</p></div>

<div id="app" style="display:grid">
<div class="grid">
<div class="card"><div class="k">Messenger</div><div class="v" id="m">-</div></div>
<div class="card"><div class="k">Bot</div><div class="v" id="b">-</div></div>
<div class="card"><div class="k">Bot uptime</div><div class="v" id="up">-</div></div>
<div class="card"><div class="k">Replies sent</div><div class="v" id="sent">0</div></div>
<div class="card"><div class="k">Target GCs</div><div class="v" id="tg">0</div></div>
<div class="card"><div class="k">Errors</div><div class="v" id="errs">0</div></div>
</div>

<div class="card"><h2>Controls</h2>
<div class="row"><button class="s" id="start">▶ Start</button><button class="r" id="stop">■ Stop</button><button class="g" id="reconnect">Reconnect</button></div>
<p class="hint">Stop = idi-disconnect ang bot sa Messenger. Hindi ito magre-reconnect hangga't hindi mo pinipindot ang Start.</p></div>

<div class="card"><h2>Facebook session</h2>
<div class="kv"><span>Account</span><span id="acc">Not linked</span></div>
<div class="kv"><span>Cookie saved</span><span id="saved">No</span></div>
<div class="kv"><span>Saved at</span><span id="savedAt">-</span></div>
<textarea id="cookie" spellcheck="false" placeholder="I-paste ang cookie dito (JSON array o c_user=...; xs=...; ...)"></textarea>
<div class="row"><button id="saveCookie">Save &amp; connect</button><button class="g" id="delCookie">Remove cookie</button></div>
<p class="hint">Ligtas ang cookie: hindi na ito ipinapakita ulit pagkatapos i-save.</p></div>

<div class="card"><h2>Target GCs</h2>
<textarea id="tids" spellcheck="false" style="min-height:60px" placeholder="Thread ID(s) ng GC - isa o marami (comma, space o bagong linya). Yung number sa messenger.com/t/&lt;ID&gt;"></textarea>
<div class="row"><button class="s" id="addT">+ Add GC(s)</button></div>
<div id="targets" style="margin-top:8px"></div>
<div class="k" style="margin-top:14px">Sagot sa mga message na walang trigger</div>
<div class="row" style="align-items:center"><input type="range" id="fb" min="0" max="100" step="5" style="flex:3"><b id="fbv" style="flex:1;text-align:right">50%</b><button class="g" id="saveFb">Save</button></div>
<p class="hint">Ang may trigger ay laging sinasagot. Ito ang chance na sasagot din sa random na message.</p></div>

<div class="card"><h2>GC Cycle (title + nicknames)</h2>
<select id="cyThread"></select>
<div class="row" style="align-items:center"><label class="chk"><input type="checkbox" id="cyName" checked> GC name</label><label class="chk"><input type="checkbox" id="cyNick" checked> Nicknames</label><label class="chk">bawat <input type="number" id="cyInt" value="3" min="2.5" step="0.5" style="width:80px"> sec</label></div>
<textarea id="cyLines" spellcheck="false" style="margin-top:10px;min-height:70px" placeholder="(optional) isang line = isang step. Kapag blangko, gagamitin ang roasts.txt"></textarea>
<div class="row"><button class="s" id="cyStart">▶ Start cycle</button><button class="r" id="cyStop">■ Stop all</button></div>
<p class="hint" id="cyInfo"></p><div id="cycles"></div></div>

<div class="card"><h2>Activity</h2><div id="ev"><div class="ev">No activity yet.</div></div></div>
<div class="row"><button class="g" id="lock">Lock dashboard</button></div>
</div>
</main>
<script>
(function(){
var $=function(i){return document.getElementById(i)};
var KEY='';try{KEY=localStorage.getItem('banat_key')||''}catch(e){}
function h(tag,props){var el=document.createElement(tag);props=props||{};
 Object.keys(props).forEach(function(k){var v=props[k];if(k==='class')el.className=v;else if(k.slice(0,2)==='on')el.addEventListener(k.slice(2),v);else el.setAttribute(k,v)});
 for(var i=2;i<arguments.length;i++){var c=arguments[i];if(c==null)continue;el.append(c.nodeType?c:document.createTextNode(String(c)))}return el}
function showErr(t){var e=$('err');e.textContent=t||'';e.style.display=t?'block':'none'}
function showLogin(){$('app').style.display='none';$('login').style.display='block'}
function api(path,method,body){
 return fetch('/api/'+path,{method:method||'GET',headers:{'Content-Type':'application/json','X-Dashboard-Key':KEY},body:body?JSON.stringify(body):undefined})
 .then(function(r){return r.json().catch(function(){return {}}).then(function(j){
  if(r.status===401){KEY='';try{localStorage.removeItem('banat_key')}catch(e){}showLogin();throw new Error('Maling dashboard key')}
  if(!r.ok||j.ok===false)throw new Error(j.detail||j.error||('HTTP '+r.status));return j})})}
function act(path,body,method){return api(path,method||'POST',body||{}).then(function(){showErr('');refresh()}).catch(function(e){showErr(e.message)})}
function fmt(s){s=+s||0;var d=Math.floor(s/86400),hh=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return(d?d+'d ':'')+(hh?hh+'h ':'')+m+'m '+(s%60)+'s'}
function dt(x){return x?new Date(x).toLocaleString():'-'}

var selectedThread='';
function render(s,ev,cy){
 $('login').style.display='none';$('app').style.display='grid';
 var on=!!s.facebook_connected;
 $('conn').textContent=on?'Connected':'Disconnected';$('conn').className='pill'+(on?' on':'');
 $('m').textContent=on?'ONLINE':'OFFLINE';$('m').className='v '+(on?'ok':'bad');
 $('b').textContent=s.bot_running?'RUNNING':(s.bot_enabled?'WAITING':'STOPPED');$('b').className='v '+(s.bot_running?'ok':(s.bot_enabled?'warn':'bad'));
 $('up').textContent=on?fmt(s.bot_uptime_seconds):'0s';
 $('sent').textContent=s.replies_sent;$('tg').textContent=s.target_gcs;$('errs').textContent=s.errors;
 var w=$('warn'),msg='';
 if(s.needs_cookie)msg='Tinanggihan ng Facebook ang session. Mag-paste ng bagong cookie. ('+(s.login_error||'')+')';
 else if(!s.session_saved)msg='Wala pang cookie. I-paste sa Facebook session sa baba.';
 else if(!s.bot_enabled)msg='Naka-stop ang bot. Pindutin ang Start.';
 else if(!on&&s.next_retry_at)msg='Nagre-reconnect... try #'+s.reconnect_attempts+' sa '+dt(s.next_retry_at)+(s.login_error?' ('+s.login_error+')':'');
 w.textContent=msg;w.style.display=msg?'block':'none';
 var ss=s.session||{};$('acc').textContent=ss.account||'Not linked';
 $('saved').textContent=ss.saved?'Yes':'No';$('saved').className=ss.saved?'ok':'bad';$('savedAt').textContent=dt(ss.saved_at);
 // targets
 var box=$('targets');box.textContent='';
 if(!s.targets.length)box.append(h('div',{class:'ev'},'No target GCs. Walang sasagutin ang bot.'));
 s.targets.forEach(function(t){box.append(h('div',{class:'kv'},h('span',null,h('b',{class:'ok'},'● ON '),t.thread_id),h('span',null,h('button',{class:'g',style:'padding:6px 10px',onclick:function(){act('targets/'+encodeURIComponent(t.thread_id),null,'DELETE')}},'Off'))))});
 var fb=$('fb');if(document.activeElement!==fb){fb.value=Math.round(s.fallback_chance*100);$('fbv').textContent=fb.value+'%'}
 // cycle thread picker
 var sel=$('cyThread');var sig=s.targets.map(function(t){return t.thread_id}).join(',');
 if(sel.dataset.sig!==sig){var cur=sel.value||selectedThread;sel.textContent='';sel.dataset.sig=sig;
  s.targets.forEach(function(t){sel.append(h('option',{value:t.thread_id},t.thread_id))});
  if(cur&&s.targets.some(function(t){return t.thread_id===cur}))sel.value=cur}
 var cbox=$('cycles');cbox.textContent='';
 (cy.cycles||[]).forEach(function(c){cbox.append(h('div',{class:'kv'},h('span',null,c.thread_id),h('span',{class:c.running?'ok':'bad'},(c.running?'● step '+c.step+'/'+c.total+' · '+c.members+' members':'stopped'+(c.note?' - '+c.note:'')))))});
 $('cyInfo').textContent=cy.roasts_lines?('roasts.txt: '+cy.roasts_lines+' line(s)'):('roasts.txt: '+(cy.roasts_note||'walang laman')+' (o maglagay ng lines sa taas)');
 // events
 var evb=$('ev');evb.textContent='';
 if(!ev.length)evb.append(h('div',{class:'ev'},'No activity yet.'));
 ev.forEach(function(e){evb.append(h('div',{class:'ev'},h('b',null,e.type),' ',e.message,h('br'),new Date(e.at).toLocaleTimeString()))});
}
function refresh(){
 if(!KEY){showLogin();return}
 Promise.all([api('status'),api('events').catch(function(){return {events:[]}}),api('cycle').catch(function(){return {cycles:[]}})])
 .then(function(r){render(r[0],r[1].events||[],r[2]);showErr('')}).catch(function(e){showErr(e.message)})}

$('unlock').onclick=function(){KEY=$('key').value.trim();try{localStorage.setItem('banat_key',KEY)}catch(e){}refresh()};
$('key').addEventListener('keydown',function(e){if(e.key==='Enter')$('unlock').click()});
$('start').onclick=function(){act('bot/start')};
$('stop').onclick=function(){act('bot/stop')};
$('reconnect').onclick=function(){act('bot/reconnect')};
$('saveCookie').onclick=function(){var c=$('cookie').value.trim();if(!c)return showErr('Mag-paste muna ng cookie.');
 api('session','POST',{cookie:c}).then(function(){$('cookie').value='';showErr('');refresh()}).catch(function(e){showErr(e.message)})};
$('delCookie').onclick=function(){if(confirm('Burahin ang saved cookie at i-disconnect?'))act('session',null,'DELETE')};
$('addT').onclick=function(){var v=$('tids').value.trim();if(!v)return showErr('Ilagay ang thread ID ng GC.');
 api('targets','POST',{thread_ids:v}).then(function(){$('tids').value='';showErr('');refresh()}).catch(function(e){showErr(e.message)})};
$('fb').oninput=function(){$('fbv').textContent=$('fb').value+'%'};
$('saveFb').onclick=function(){act('settings',{fallback_chance:$('fb').value/100},'PUT')};
$('cyThread').onchange=function(){selectedThread=this.value};
$('cyStart').onclick=function(){var t=$('cyThread').value;if(!t)return showErr('Magdagdag muna ng target GC.');
 var lines=$('cyLines').value.split('\n').map(function(x){return x.trim()}).filter(Boolean);
 act('cycle/start',{thread_id:t,lines:lines.length?lines:null,do_name:$('cyName').checked,do_nick:$('cyNick').checked,interval_seconds:+$('cyInt').value||3})};
$('cyStop').onclick=function(){act('cycle/stop',{})};
$('lock').onclick=function(){try{localStorage.removeItem('banat_key')}catch(e){}KEY='';location.reload()};
refresh();setInterval(function(){if(KEY)refresh()},4000);
})();
</script></body></html>
"""
