from __future__ import annotations

import hmac
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from os import environ
from threading import Thread
from urllib.parse import parse_qs, urlparse

from .control import ControlHub
from .utils import debugger

DASHBOARD_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autofishbot Dashboard</title>
<style>
  :root{
    --bg:#0d1117;--card:#161b22;--card2:#1c2330;--border:#2b3444;
    --text:#e6edf3;--muted:#8b949e;--accent:#58a6ff;
    --ok:#3fb950;--warn:#d29922;--err:#f85149;--mag:#bc8cff;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--text);line-height:1.45;padding:16px}
  .wrap{max-width:1140px;margin:0 auto}
  header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:16px;padding:14px 18px;background:var(--card);border:1px solid var(--border);border-radius:12px}
  .brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:1.1rem}
  .dot{width:10px;height:10px;border-radius:50%;background:var(--ok);box-shadow:0 0 8px var(--ok)}
  .meta{color:var(--muted);font-size:.85rem;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
  .tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
  .tab{background:var(--card);border:1px solid var(--border);color:var(--muted);padding:7px 13px;border-radius:8px;cursor:pointer;font-size:.88rem;font-weight:600}
  .tab.active{background:var(--accent);color:#09111b;border-color:var(--accent)}
  .badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.74rem;font-weight:700;letter-spacing:.03em}
  .st-FISHING{background:rgba(63,185,80,.18);color:var(--ok)}
  .st-PAUSED,.st-WAITING{background:rgba(210,153,34,.18);color:var(--warn)}
  .st-CAPTCHA,.st-DEAD{background:rgba(248,81,73,.2);color:var(--err)}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:14px}
  .kpi{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
  .kpi .k{color:var(--muted);font-size:.76rem;text-transform:uppercase;letter-spacing:.04em}
  .kpi .v{font-size:1.22rem;font-weight:700;margin-top:3px}
  .cols{display:grid;grid-template-columns:1fr 1.15fr;gap:14px}
  @media(max-width:820px){.cols{grid-template-columns:1fr}}
  .panel{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:15px}
  .panel-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:12px;flex-wrap:wrap}
  .panel h2{font-size:.88rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
  .subtabs{display:flex;gap:6px}
  .subtab{background:var(--card2);border:1px solid var(--border);color:var(--muted);padding:5px 10px;border-radius:7px;cursor:pointer;font-size:.78rem;font-weight:600}
  .subtab.active{border-color:var(--accent);color:var(--accent)}
  .actions{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}
  button.btn{background:var(--card2);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:8px;cursor:pointer;font-size:.84rem;font-weight:600;transition:.15s}
  button.btn:hover{border-color:var(--accent);color:var(--accent)}
  button.btn.primary{background:var(--accent);color:#09111b;border-color:var(--accent)}
  button.btn.warn{background:rgba(210,153,34,.15);border-color:var(--warn);color:var(--warn)}
  .toggle-row{display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid rgba(43,52,68,.55)}
  .toggle-row:last-child{border-bottom:0}
  .pill{padding:4px 11px;border-radius:999px;font-size:.76rem;font-weight:700;border:1px solid var(--border);cursor:pointer}
  .pill.on{background:rgba(63,185,80,.2);color:var(--ok);border-color:var(--ok)}
  .pill.off{background:var(--card2);color:var(--muted)}
  .filter-input{width:100%;padding:7px 10px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:.82rem;margin-bottom:10px}
  .feed{list-style:none;max-height:360px;overflow:auto;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:.8rem}
  .feed li{padding:5px 0;border-bottom:1px solid rgba(43,52,68,.45);display:flex;gap:10px;word-break:break-word}
  .feed .ts{color:var(--muted);flex-shrink:0}
  .lvl-err{color:var(--err)}
  .lvl-warn{color:var(--warn)}
  .lvl-info{color:var(--accent)}
  .notif{background:var(--card2);border:1px solid var(--border);border-radius:8px;padding:9px 12px;margin-bottom:14px;font-size:.85rem;color:var(--muted)}
  .toast{position:fixed;right:18px;bottom:18px;background:#1f2937;border:1px solid var(--accent);color:var(--text);padding:10px 14px;border-radius:10px;font-size:.86rem;opacity:0;pointer-events:none;transition:.2s}
  .toast.show{opacity:1}
  .auth-box{max-width:360px;margin:80px auto;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:22px}
  .auth-box input{width:100%;padding:10px 12px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--text);margin:12px 0}
  .warn-banner{background:rgba(210,153,34,.12);border:1px solid rgba(210,153,34,.4);color:var(--warn);padding:8px 12px;border-radius:8px;font-size:.8rem;margin-bottom:12px}
</style>
</head>
<body>
<div id="auth" class="auth-box" style="display:none">
  <h2 style="margin-bottom:6px">Autofishbot Dashboard</h2>
  <p style="color:var(--muted);font-size:.86rem">Masukkan password dashboard (<code>AFB_WEB_PASSWORD</code>).</p>
  <input id="pw" type="password" placeholder="Password" autocomplete="current-password">
  <button class="btn primary" style="width:100%" onclick="saveAuth()">Masuk</button>
  <p id="authErr" style="color:var(--err);font-size:.82rem;margin-top:8px"></p>
</div>

<div id="app" class="wrap" style="display:none">
  <header>
    <div class="brand"><span class="dot"></span> Autofishbot Dashboard</div>
    <div class="meta">
      <span id="hubUp">Uptime: -</span>
      <span id="lastSync">Sync: -</span>
      <button class="btn" onclick="refresh()">Refresh</button>
    </div>
  </header>
  <div id="noAuthWarn" class="warn-banner" style="display:none">
    Dashboard berjalan tanpa password. Disarankan set <code>AFB_WEB_PASSWORD</code> di Pterodactyl Startup atau <code>[WEB] PASSWORD</code> di config.
  </div>
  <div id="tabs" class="tabs"></div>
  <div id="notif" class="notif">No recent notification.</div>
  <div id="kpis" class="grid"></div>
  <div class="cols">
    <div class="panel">
      <div class="panel-head"><h2>Controls &amp; Automation</h2></div>
      <div id="actions" class="actions"></div>
      <div id="toggles"></div>
    </div>
    <div class="panel">
      <div class="panel-head">
        <h2 id="rightTitle">Live Activity</h2>
        <div class="subtabs">
          <button id="tabCatches" class="subtab active" onclick="setRightTab('catches')">Catches</button>
          <button id="tabLogs" class="subtab" onclick="setRightTab('logs')">Live Logs</button>
        </div>
      </div>
      <input id="logFilter" class="filter-input" style="display:none" placeholder="Filter logs (e.g. error, watchdog, captcha, 429)..." oninput="renderRight()">
      <ul id="feed" class="feed"></ul>
    </div>
  </div>
</div>
<div id="toast" class="toast"></div>

<script>
const TOGGLE_META = [
  ['more_fish','[1] More Fish'],
  ['more_treasures','[2] More Treasures'],
  ['auto_daily','[3] Auto Daily'],
  ['auto_sell','[4] Auto Sell'],
  ['auto_update_inventory','[5] Auto Update Inv'],
  ['auto_buy_baits','[6] Auto Buy Baits'],
  ['fish_on_exit','[7] Fish On Exit']
];
let stateData = null;
let activeName = null;
let rightTab = 'catches';
let token = sessionStorage.getItem('afb_web_token') || '';

function headers(){
  const h = {'Content-Type':'application/json'};
  if(token) h['Authorization'] = 'Bearer ' + token;
  return h;
}
function toast(msg){
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(()=>el.classList.remove('show'), 2600);
}
function saveAuth(){
  token = document.getElementById('pw').value.trim();
  sessionStorage.setItem('afb_web_token', token);
  document.getElementById('authErr').textContent = '';
  refresh();
}
function esc(s){
  return String(s===null||s===undefined?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function money(v){ return v===null || v===undefined ? '-' : '$' + Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}); }
function logClass(text){
  if(text.startsWith('[E]')) return 'lvl-err';
  if(text.startsWith('[!]') || text.startsWith('[429]')) return 'lvl-warn';
  if(text.startsWith('[*]')) return 'lvl-info';
  return '';
}
function setRightTab(tab){
  rightTab = tab;
  document.getElementById('tabCatches').classList.toggle('active', tab==='catches');
  document.getElementById('tabLogs').classList.toggle('active', tab==='logs');
  document.getElementById('logFilter').style.display = tab==='logs' ? 'block' : 'none';
  renderRight();
}

async function refresh(){
  try{
    const res = await fetch('/api/status', {headers: headers()});
    if(res.status === 401){
      document.getElementById('app').style.display='none';
      document.getElementById('auth').style.display='block';
      if(token) document.getElementById('authErr').textContent='Password salah.';
      return;
    }
    const data = await res.json();
    stateData = data;
    document.getElementById('auth').style.display='none';
    document.getElementById('app').style.display='block';
    document.getElementById('noAuthWarn').style.display = data.auth_enabled ? 'none' : 'block';
    document.getElementById('hubUp').textContent = 'Uptime: ' + data.uptime;
    document.getElementById('lastSync').textContent = 'Sync: ' + new Date().toLocaleTimeString();
    render();
  }catch(e){
    toast('Connection error: ' + e.message);
  }
}

async function doAction(action, feature){
  if(!activeName) return;
  try{
    const res = await fetch('/api/action', {
      method:'POST',
      headers: headers(),
      body: JSON.stringify({account: activeName, action, feature})
    });
    const d = await res.json();
    toast(d.message || (d.ok ? 'OK' : 'Failed'));
    refresh();
  }catch(e){
    toast('Action failed: ' + e.message);
  }
}

function activeAccount(){
  const accs = (stateData && stateData.accounts) || [];
  if(!accs.length) return null;
  if(!activeName || !accs.some(a=>a.name===activeName)) activeName = accs[0].name;
  return accs.find(a=>a.name===activeName) || accs[0];
}

function renderRight(){
  const acc = activeAccount();
  if(!acc) return;
  const feed = document.getElementById('feed');
  const logs = acc.logs || [];
  document.getElementById('tabLogs').textContent = `Live Logs (${logs.length})`;

  if(rightTab === 'catches'){
    document.getElementById('rightTitle').textContent = 'Recent Catches';
    if(!acc.recent_catches || !acc.recent_catches.length){
      feed.innerHTML = '<li style="color:var(--muted)">No catches logged yet.</li>';
    } else {
      feed.innerHTML = acc.recent_catches.map(r=>`<li><span class="ts">${esc(r.time)}</span><span>${esc(r.text)}</span></li>`).join('');
    }
  } else {
    document.getElementById('rightTitle').textContent = 'Live Logs (Ring Buffer)';
    const q = (document.getElementById('logFilter').value || '').trim().toLowerCase();
    const filtered = logs.slice().reverse().filter(r => !q || (r.text || '').toLowerCase().includes(q));
    if(!filtered.length){
      feed.innerHTML = '<li style="color:var(--muted)">No matching log entries.</li>';
    } else {
      feed.innerHTML = filtered.map(r=>`<li><span class="ts">${esc(r.time)}</span><span class="${logClass(r.text||'')}">${esc(r.text)}</span></li>`).join('');
    }
  }
}

function render(){
  const accs = (stateData && stateData.accounts) || [];
  const acc = activeAccount();
  if(!acc) return;

  const tabs = document.getElementById('tabs');
  tabs.innerHTML = accs.map((a, idx) =>
    `<button class="tab ${a.name===activeName?'active':''}" onclick="selectAcc(${idx})">
      ${esc(a.name)}${a.username?' ('+esc(a.username)+')':''} <span class="badge st-${esc(a.state)}">${esc(a.state)}</span>
    </button>`
  ).join('');

  document.getElementById('notif').textContent = acc.error ? ('Error: ' + acc.error) : (acc.notification || 'No recent notification.');

  const kpis = [
    ['State', `<span class="badge st-${esc(acc.state)}">${esc(acc.state)}</span>`],
    ['Fish Caught', `${esc(acc.fish)} <span style="font-size:.82rem;color:var(--muted)">(${esc(acc.rate_per_min)}/m)</span>`],
    ['Catch / Hr', esc(acc.catch_per_hour)],
    ['Streak', esc(acc.streak)],
    ['Balance', esc(acc.balance)],
    ['Balance / Hr', acc.balance_per_hour !== null ? money(acc.balance_per_hour)+'/h' : '-'],
    ['Auto Sell', `${money(acc.auto_sell_total)} <span style="font-size:.8rem;color:var(--muted)">(${esc(acc.auto_sell_count)}x)</span>`],
    ['Watchdog / Idle', `${esc(acc.watchdog_recoveries)}x <span style="font-size:.8rem;color:var(--muted)">(${esc(acc.last_catch_age_sec)}s)</span>`]
  ];
  document.getElementById('kpis').innerHTML = kpis.map(([k,v])=>`<div class="kpi"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');

  document.getElementById('actions').innerHTML = `
    <button class="btn ${acc.paused?'primary':'warn'}" onclick="doAction('${acc.paused?'resume':'pause'}')">${acc.paused?'Resume':'Pause'}</button>
    <button class="btn" onclick="doAction('sell')">Sell</button>
    <button class="btn" onclick="doAction('daily')">Daily</button>
    <button class="btn" onclick="doAction('inventory')">Update Inv</button>
    <button class="btn" onclick="doAction('more_fish')">More Fish</button>
    <button class="btn" onclick="doAction('more_treasures')">More Treasures</button>
    <button class="btn" onclick="doAction('save')">Save Toggles</button>
  `;

  document.getElementById('toggles').innerHTML = TOGGLE_META.map(([feat, label])=>{
    const on = !!(acc.toggles && acc.toggles[feat]);
    return `<div class="toggle-row">
      <span>${esc(label)}</span>
      <button class="pill ${on?'on':'off'}" onclick="doAction('toggle','${feat}')">${on?'ON':'OFF'}</button>
    </div>`;
  }).join('');

  renderRight();
}

function selectAcc(idx){
  const accs = (stateData && stateData.accounts) || [];
  if(accs[idx]){ activeName = accs[idx].name; render(); }
}

document.getElementById('pw').addEventListener('keydown', e => { if(e.key==='Enter') saveAuth(); });
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
'''


class WebDashboard:
    '''Optional lightweight stdlib HTTP dashboard designed for Pterodactyl allocated ports.'''

    def __init__(self, hub: ControlHub, host: str = '0.0.0.0', port: int = 8080, password: str = '') -> None:
        self.hub = hub
        self.host = host
        self.port = int(port)
        self.password = password.strip()
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    @classmethod
    def from_configs(cls, configs: list, hub: ControlHub) -> WebDashboard | None:
        '''Resolves dashboard settings from env vars and config [WEB] section.'''
        env_flag = environ.get('AFB_WEB', '').strip().lower()
        env_port = environ.get('AFB_WEB_PORT', '').strip()
        ptero_port = environ.get('SERVER_PORT', '').strip()

        cfg_enabled = any(bool(getattr(c, 'web_enabled', False)) for c in configs)
        if env_flag in ('0', 'false', 'off', 'no'):
            return None
        enabled = (env_flag in ('1', 'true', 'on', 'yes')) or bool(env_port) or cfg_enabled
        if not enabled:
            return None

        host = environ.get('AFB_WEB_HOST', '').strip()
        if not host:
            for c in configs:
                h = str(getattr(c, 'web_host', '') or '').strip()
                if h:
                    host = h
                    break
        host = host or '0.0.0.0'

        port = 0
        for raw in (env_port, *[str(getattr(c, 'web_port', 0) or '') for c in configs], ptero_port, '8080'):
            if raw and raw.strip().isdigit() and int(raw.strip()) > 0:
                port = int(raw.strip())
                break

        password = environ.get('AFB_WEB_PASSWORD', '').strip()
        if not password:
            for c in configs:
                pw = str(getattr(c, 'web_password', '') or '').strip()
                if pw:
                    password = pw
                    break

        return cls(hub=hub, host=host, port=port, password=password)

    def _authorized(self, headers) -> bool:
        if not self.password:
            return True
        auth = str(headers.get('Authorization', '') or '').strip()
        if auth.lower().startswith('bearer '):
            candidate = auth[7:].strip()
        else:
            candidate = str(headers.get('X-API-Key', '') or '').strip()
        return bool(candidate) and hmac.compare_digest(candidate, self.password)

    def start(self) -> bool:
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def _send_json(self, code: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_html(self, html: str) -> None:
                body = html.encode('utf-8')
                self.send_response(HTTPStatus.OK)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path
                if path == '/health':
                    self._send_json(HTTPStatus.OK, {'status': 'ok', 'accounts': len(dashboard.hub.accounts)})
                    return
                if path in ('/', '/index.html'):
                    self._send_html(DASHBOARD_HTML)
                    return
                if path == '/api/status':
                    if not dashboard._authorized(self.headers):
                        self._send_json(HTTPStatus.UNAUTHORIZED, {'error': 'unauthorized'})
                        return
                    payload = dashboard.hub.snapshot_all()
                    payload['auth_enabled'] = bool(dashboard.password)
                    self._send_json(HTTPStatus.OK, payload)
                    return
                if path == '/api/logs':
                    if not dashboard._authorized(self.headers):
                        self._send_json(HTTPStatus.UNAUTHORIZED, {'error': 'unauthorized'})
                        return
                    qs = parse_qs(parsed.query)
                    acc_name = (qs.get('account') or [None])[0]
                    raw_limit = (qs.get('limit') or ['100'])[0]
                    limit = int(raw_limit) if str(raw_limit).isdigit() else 100
                    acc = dashboard.hub.resolve(acc_name)
                    if not acc:
                        self._send_json(HTTPStatus.NOT_FOUND, {'error': 'account not found'})
                        return
                    self._send_json(HTTPStatus.OK, {'account': acc.name, 'logs': acc.recent_logs(limit)})
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {'error': 'not found'})

            def do_POST(self):
                path = urlparse(self.path).path
                if path != '/api/action':
                    self._send_json(HTTPStatus.NOT_FOUND, {'error': 'not found'})
                    return
                if not dashboard._authorized(self.headers):
                    self._send_json(HTTPStatus.UNAUTHORIZED, {'error': 'unauthorized'})
                    return
                try:
                    length = min(int(self.headers.get('Content-Length', '0') or '0'), 8192)
                    raw = self.rfile.read(length) if length > 0 else b'{}'
                    body = json.loads(raw.decode('utf-8'))
                except Exception:
                    self._send_json(HTTPStatus.BAD_REQUEST, {'ok': False, 'message': 'Invalid JSON payload.'})
                    return

                acc = dashboard.hub.resolve(body.get('account'))
                if not acc:
                    self._send_json(HTTPStatus.NOT_FOUND, {'ok': False, 'message': 'Account not found.'})
                    return
                action = str(body.get('action') or '').strip().lower()
                if action == 'toggle':
                    ok, msg = acc.toggle_feature(str(body.get('feature') or ''))
                else:
                    ok, msg = acc.trigger_action(action)
                self._send_json(HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST, {'ok': ok, 'message': msg})

        try:
            self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError as e:
            debugger.log(e, 'WebDashboard - bind')
            print(f'[E] Failed to start Web Dashboard on {self.host}:{self.port}: {e}', flush=True)
            return False

        self._thread = Thread(target=self._server.serve_forever, daemon=True, name='Web dashboard')
        self._thread.start()
        auth_note = 'password-protected' if self.password else 'NO PASSWORD (set AFB_WEB_PASSWORD!)'
        print(f'[*] Web dashboard listening on http://{self.host}:{self.port} ({auth_note})', flush=True)
        return True

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
