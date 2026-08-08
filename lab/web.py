"""Control panel for the lab.

The point of this is economic: driving scenarios through an assistant costs a conversation
every time, and a scenario run is a fixed sequence that does not need one. This page runs the
same functions the CLI does, so routine work costs nothing.

It binds to the machine running it, which must be able to reach both the hypervisor and the
lab subnet. Deliberately no authentication -- put it somewhere only you can reach, and do not
expose it beyond your own network. It can revert VMs and run code inside guests.
"""
import asyncio
import html

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from . import config, core, scenarios, sensor

app = FastAPI(title="Falcon lab")


async def _off(fn, *a, **kw):
    """Everything here shells out; keep it off the event loop."""
    return await asyncio.to_thread(fn, *a, **kw)


@app.get("/api/status")
async def api_status():
    out = []
    for name in list(config.HOSTS):
        st = await _off(core.status, name)
        if name in config.GUESTS:
            st["sensor"] = await _off(sensor.verify, name)
        out.append(st)
    return JSONResponse(out)


@app.get("/api/scenarios")
async def api_scenarios():
    all_ = await _off(scenarios.load_all)
    return JSONResponse([
        {k: v for k, v in s.items() if k != "steps"} for s in
        sorted(all_.values(), key=lambda x: x["_file"])
    ])


@app.post("/api/run/{sid}")
async def api_run(sid: str, no_revert: bool = False):
    try:
        return JSONResponse(await _off(scenarios.run, sid, no_revert))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/grade/{sid}")
async def api_grade(sid: str):
    try:
        return JSONResponse(await _off(scenarios.grade, sid))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/revert/{name}/{baseline}")
async def api_revert(name: str, baseline: str):
    try:
        return JSONResponse(await _off(core.revert, name, baseline))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


PAGE = """
<title>Falcon lab</title>
<style>
  :root{--bg:#f4f5f7;--card:#fff;--ink:#15181d;--dim:#5d6672;--line:#d9dee5;
        --ok:#2c7a52;--bad:#a33528;--warn:#8a5a12;--accent:#1f4f8f}
  @media (prefers-color-scheme:dark){:root{--bg:#12151a;--card:#1a1f26;--ink:#e6e9ee;
    --dim:#98a2b0;--line:#2c333d;--ok:#5cc08a;--bad:#e58072;--warn:#d9a44a;--accent:#7aa9e8}}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:15px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .wrap{max-width:980px;margin:0 auto;padding:28px 20px 60px}
  h1{font-size:20px;margin:0 0 4px;letter-spacing:-.01em}
  .sub{color:var(--dim);font-size:13px;margin-bottom:22px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:8px;
        padding:14px 16px;margin-bottom:12px}
  .row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
  .pill{font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;border:1px solid var(--line)}
  .ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)} .dim{color:var(--dim)}
  button{font:inherit;font-size:13px;font-weight:550;padding:6px 13px;border-radius:6px;
         border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
  button:hover{border-color:var(--accent);color:var(--accent)}
  button:disabled{opacity:.5;cursor:default}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  h2{font-size:15px;margin:26px 0 10px}
  .name{font-weight:600}
  .meta{color:var(--dim);font-size:12.5px}
  pre{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:10px 12px;
      overflow-x:auto;font-size:12.5px;margin:10px 0 0;white-space:pre-wrap}
  .spacer{flex:1}
</style>
<div class="wrap">
  <h1>Falcon lab</h1>
  <div class="sub">Scenario runner. Reverting rolls a guest back to a clean baseline in seconds.</div>
  <div id="hosts"></div>
  <h2>Scenarios</h2>
  <div id="scen"></div>
</div>
<script>
const $ = s => document.querySelector(s);
const esc = s => (s??'').toString().replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));

function sensorClass(v){
  if(!v) return 'dim';
  if(v.includes('healthy')) return 'ok';
  if(v.includes('NOT installed')) return 'dim';
  return 'warn';
}

async function loadHosts(){
  const r = await (await fetch('/api/status')).json();
  $('#hosts').innerHTML = r.map(h => `
    <div class="card"><div class="row">
      <span class="name">${esc(h.host)}</span>
      <span class="mono dim">${esc(h.ip)}</span>
      <span class="pill ${h.reachable?'ok':'dim'}">${h.reachable?'up':esc(h.vm)}</span>
      ${h.sensor?`<span class="${sensorClass(h.sensor.verdict)}">${esc(h.sensor.verdict)}</span>`:''}
      <span class="spacer"></span>
      ${h.snapshots.length?h.snapshots.map(s=>
        `<button onclick="revert('${h.host}','${s}',this)">revert ${esc(s)}</button>`).join(''):''}
    </div></div>`).join('');
}

async function revert(host, snap, btn){
  const base = snap.includes('Sensor')&&!snap.includes('pre') ? 'sensor' : 'bare';
  btn.disabled = true; btn.textContent = 'reverting...';
  await fetch(`/api/revert/${host}/${base}`, {method:'POST'});
  await loadHosts();
}

async function loadScen(){
  const r = await (await fetch('/api/scenarios')).json();
  $('#scen').innerHTML = r.map(s => `
    <div class="card">
      <div class="row">
        <span class="name">${esc(s.name)}</span>
        <span class="pill ${s.mode==='manual'?'warn':'dim'}">${esc(s.mode)}</span>
        <span class="meta">${esc(s.target)} &middot; baseline ${esc(s.baseline)}</span>
        <span class="spacer"></span>
        <button class="primary" onclick="run('${s.id}',this)">
          ${s.mode==='manual'?'set up':'run'}</button>
        <button onclick="grade('${s.id}',this)">grade</button>
      </div>
      <div class="meta" style="margin-top:6px">${esc(s.summary||'')}</div>
      <pre id="out-${s.id}" style="display:none"></pre>
    </div>`).join('');
}

function show(id, text){
  const el = $('#out-'+id); el.style.display='block'; el.textContent = text;
}

async function run(id, btn){
  btn.disabled=true; const t=btn.textContent; btn.textContent='working...';
  const r = await (await fetch(`/api/run/${id}`, {method:'POST'})).json();
  btn.disabled=false; btn.textContent=t;
  if(r.error){ show(id, 'error: '+r.error); return; }
  let out = '';
  if(r.prepared) out += `baseline -> ${r.prepared.snapshot} (${r.prepared.ready?'ready':'NOT READY'})\\n\\n`;
  if(r.mode==='manual'){ out += r.instructions || ''; }
  else {
    out += (r.steps||[]).map(s=>`[${s.n}] ${s.name}  ${s.ok===true?'ok':s.ok===false?'FAIL':'--'}`+
      (s.out?`\\n     ${s.out.split('\\n').slice(0,4).join('\\n     ')}`:'')).join('\\n');
    if(r.expect && r.expect.console) out += `\\n\\nLook in Falcon: ${r.expect.console.trim()}`;
  }
  show(id, out);
  loadHosts();
}

async function grade(id, btn){
  btn.disabled=true; btn.textContent='checking...';
  const r = await (await fetch(`/api/grade/${id}`, {method:'POST'})).json();
  btn.disabled=false; btn.textContent='grade';
  if(r.error){ show(id,'error: '+r.error); return; }
  const verdict = r.passed===true?'PASS':r.passed===false?'NOT YET':'--';
  show(id, `${verdict}  ${r.verdict||''}` + (r.hint?`\\n\\n${r.hint}`:''));
  loadHosts();
}

loadHosts(); loadScen();
setInterval(loadHosts, 20000);
</script>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(PAGE)
