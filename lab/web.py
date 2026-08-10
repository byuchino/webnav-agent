"""Control panel for the lab.

The point is economic: driving scenarios through an assistant costs a conversation every
time, and a scenario run is a fixed sequence that does not need one. This page calls the same
functions the CLI does, so routine work costs nothing at all.

It must run somewhere that can reach both the hypervisor and the lab subnet.

> **No authentication, deliberately.** It can revert VMs and execute code inside guests. Put
> it somewhere only you can reach and do not expose it beyond your own network.
"""
import asyncio
import threading
import time
import uuid

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from . import config, core, scenarios, sensor

app = FastAPI(title="Falcon lab")

DOMAINS = {
    0: "Foundations", 1: "User Management", 2: "Sensor Deployment",
    3: "Host Management", 4: "Group Creation", 5: "Policy Application",
    6: "Rules Configuration", 7: "Dashboards and Reports", 8: "Workflows",
}


async def _off(fn, *a, **kw):
    """Everything here shells out; keep it off the event loop."""
    return await asyncio.to_thread(fn, *a, **kw)


# A long POST that returns only when finished is indistinguishable from a hang. Runs become
# jobs: start one, poll it, watch the stages report themselves as they happen.
JOBS = {}
_LOCK = threading.Lock()


def _new_job():
    jid = uuid.uuid4().hex[:12]
    with _LOCK:
        JOBS[jid] = {"lines": [], "done": False, "result": None, "started": time.time()}
    # Keep the last few only; this is a lab panel, not a job server.
    with _LOCK:
        for old in sorted(JOBS, key=lambda k: JOBS[k]["started"])[:-12]:
            JOBS.pop(old, None)
    return jid


def _say(jid, msg):
    with _LOCK:
        j = JOBS.get(jid)
        if j is not None:
            j["lines"].append({"t": round(time.time() - j["started"]), "m": str(msg)})


def _run_job(jid, fn, *a, **kw):
    try:
        res = fn(*a, progress=lambda m: _say(jid, m), **kw)
        with _LOCK:
            JOBS[jid]["result"] = res
    except Exception as e:  # noqa: BLE001
        _say(jid, f"ERROR: {e}")
        with _LOCK:
            JOBS[jid]["result"] = {"error": str(e)}
    finally:
        with _LOCK:
            JOBS[jid]["done"] = True


@app.get("/api/job/{jid}")
async def api_job(jid: str):
    with _LOCK:
        j = JOBS.get(jid)
        if not j:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        return JSONResponse({"lines": list(j["lines"]), "done": j["done"],
                             "result": j["result"]})


@app.get("/api/status")
async def api_status():
    out = []
    for name in list(config.HOSTS):
        st = await _off(core.status, name)
        h = config.HOSTS[name]
        have = set(st.get("snapshots") or [])
        # Report BASELINES, not raw snapshot names: the UI should speak the same vocabulary
        # the scenarios do, rather than making the page guess which snapshot means what.
        st["baselines"] = [b for b, snap in (h.get("snapshots") or {}).items() if snap in have]
        if name in config.GUESTS:
            st["sensor"] = await _off(sensor.verify, name)
        out.append(st)
    return JSONResponse(out)


@app.get("/api/scenarios")
async def api_scenarios():
    all_ = await _off(scenarios.load_all)
    out = []
    for s in sorted(all_.values(), key=lambda x: (x.get("domain", 0), x["id"])):
        d = {k: v for k, v in s.items() if k not in ("steps", "setup")}
        d["kinds"] = sorted({v["kind"] for v in (s.get("verify") or [])})
        d["domain_name"] = DOMAINS.get(s.get("domain", 0), "?")
        d["prep"] = scenarios.prep_steps(s)
        out.append(d)
    return JSONResponse(out)


@app.post("/api/run/{sid}")
async def api_run(sid: str, no_revert: bool = False):
    """Returns immediately with a job id; poll /api/job/<id> for progress."""
    try:
        scenarios.get(sid)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)
    jid = _new_job()
    threading.Thread(target=_run_job, args=(jid, scenarios.run, sid, no_revert),
                     daemon=True).start()
    return JSONResponse({"job": jid})


@app.post("/api/revert-job/{name}/{baseline}")
async def api_revert_job(name: str, baseline: str):
    jid = _new_job()
    threading.Thread(target=_run_job, args=(jid, core.revert, name, baseline),
                     daemon=True).start()
    return JSONResponse({"job": jid})


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


PAGE = r"""
<title>Falcon lab</title>
<style>
  :root{--bg:#f4f5f7;--card:#fff;--ink:#15181d;--dim:#5d6672;--line:#d9dee5;
        --ok:#2c7a52;--bad:#a33528;--warn:#8a5a12;--accent:#1f4f8f;--soft:#eef1f4}
  @media (prefers-color-scheme:dark){:root{--bg:#12151a;--card:#1a1f26;--ink:#e6e9ee;
    --dim:#98a2b0;--line:#2c333d;--ok:#5cc08a;--bad:#e58072;--warn:#d9a44a;
    --accent:#7aa9e8;--soft:#20262e}}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:15px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .wrap{max-width:1000px;margin:0 auto;padding:26px 20px 70px}
  h1{font-size:20px;margin:0 0 4px;letter-spacing:-.01em}
  .sub{color:var(--dim);font-size:13px;margin-bottom:20px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:8px;
        padding:13px 15px;margin-bottom:10px}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
  .pill{font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;
        border:1px solid var(--line);color:var(--dim)}
  .ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)} .dim{color:var(--dim)}
  button{font:inherit;font-size:13px;font-weight:550;padding:5px 12px;border-radius:6px;
         border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
  button:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
  button:disabled{opacity:.45;cursor:default}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  button.primary:hover:not(:disabled){color:#fff;opacity:.9}
  h2{font-size:12px;margin:24px 0 8px;text-transform:uppercase;letter-spacing:.09em;
     color:var(--dim);font-weight:650}
  .name{font-weight:600}
  .meta{color:var(--dim);font-size:12.5px}
  .spacer{flex:1}
  pre{background:var(--soft);border:1px solid var(--line);border-radius:6px;padding:10px 12px;
      overflow-x:auto;font-size:12.5px;margin:10px 0 0;white-space:pre-wrap;line-height:1.5}
  .checks{margin:10px 0 0}
  .chk{display:flex;gap:9px;align-items:baseline;padding:3px 0;font-size:13.5px}
  .chk .m{width:74px;flex:none;font-weight:650;font-size:11.5px}
  .chk .why{color:var(--dim);font-size:12.5px}
  .attest{margin:2px 0 0 83px;color:var(--dim);font-size:12.5px}
  .attest div{padding:1px 0}
  .verdict{font-weight:650;font-size:13px;margin-top:8px}
  details.prep{margin-top:7px;font-size:12.5px;color:var(--dim)}
  details.prep summary{cursor:pointer;user-select:none}
  details.prep ul{margin:6px 0 0;padding-left:20px}
  details.prep li{padding:1px 0}
</style>
<div class="wrap">
  <h1>Falcon lab</h1>
  <div class="sub">Reverting rolls a guest back to a clean baseline in seconds. Grading reports
    what is actually true &mdash; a check that could not run is never a pass.</div>
  <div id="hosts"></div>
  <div id="out-hosts-log"></div>
  <div id="scen"></div>
</div>
<script>
const $ = s => document.querySelector(s);
const esc = s => (s??'').toString().replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));

function sensorClass(v){
  if(!v) return 'dim';
  if(v.includes('healthy')) return 'ok';
  if(v.includes('NOT installed')) return 'dim';
  return 'warn';
}
const MARK = {true:['ok','ok'], false:['FAIL','bad'], null:['--','dim']};

async function loadHosts(){
  const r = await (await fetch('/api/status')).json();
  $('#hosts').innerHTML = '<h2>Hosts</h2>' + r.map(h => `
    <div class="card"><div class="row">
      <span class="name">${esc(h.host)}</span>
      <span class="mono dim">${esc(h.ip)}</span>
      <span class="pill ${h.reachable?'ok':'dim'}">${h.reachable?'up':esc(h.vm)}</span>
      ${h.sensor?`<span class="${sensorClass(h.sensor.verdict)}">${esc(h.sensor.verdict)}</span>`:''}
      <span class="spacer"></span>
      ${(h.baselines||[]).map(b=>
        `<button onclick="revert('${h.host}','${b}',this)">revert to ${esc(b)}</button>`).join('')}
    </div></div>`).join('');
}

async function revert(host, baseline, btn){
  if(!confirm(`Revert ${host} to the "${baseline}" baseline? Current state is discarded.`)) return;
  btn.disabled=true; const t=btn.textContent;
  const poll = setInterval(loadHosts, 8000);
  try{
    const start = await (await fetch(`/api/revert-job/${host}/${baseline}`,{method:'POST'})).json();
    const r = await follow(start.job, 'hosts-log', btn, 'reverting');
    if(r.error) alert(r.error);
  } catch(e){ alert(e.message); }
  finally { clearInterval(poll); btn.disabled=false; btn.textContent=t; loadHosts(); }
}

async function loadScen(){
  const r = await (await fetch('/api/scenarios')).json();
  let html = '', dom = null;
  for(const s of r){
    if(s.domain !== dom){ dom = s.domain; html += `<h2>${s.domain}. ${esc(s.domain_name)}</h2>`; }
    const req = (s.requires||[]).length ? `<span class="meta">needs ${esc((s.requires||[]).join(', '))}</span>` : '';
    html += `
    <div class="card" data-target="${esc(s.target)}" data-baseline="${esc(s.baseline||'none')}">
      <div class="row">
        <span class="name">${esc(s.name)}</span>
        <span class="mono dim">${esc(s.id)}</span>
        <span class="pill ${s.mode==='auto'?'':'warn'}">${esc(s.mode)}</span>
        <span class="pill">${esc(s.target)}</span>
        ${s.objective?`<span class="meta">${esc(s.objective)}</span>`:''}
        <span class="spacer"></span>
        <button class="primary" onclick="run('${s.id}',this)">
          ${s.mode==='auto'?'run':'set up'}</button>
        <button onclick="grade('${s.id}',this)">grade</button>
      </div>
      <div class="meta" style="margin-top:5px">${esc(s.summary||'')}</div>
      <details class="prep"><summary>what pressing
        ${s.mode==='auto'?'run':'set up'} will do</summary>
        <ul>${(s.prep||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ul></details>
      <div class="row" style="margin-top:5px">
        <span class="meta mono">${esc((s.kinds||[]).join(' + ')) || 'no checks'}</span>
        ${s.duration_min?`<span class="meta">~${s.duration_min} min</span>`:''}
        ${req}
      </div>
      <div id="out-${s.id}"></div>
    </div>`;
  }
  $('#scen').innerHTML = html;
}

function box(id, inner){ $('#out-'+id).innerHTML = inner; }

// Long operations need a floor under them: a revert plus a Windows boot is minutes, and a
// frozen button is indistinguishable from a crash.
async function call(url, btn, label, secs){
  const t = btn.textContent;
  const started = Date.now();
  const tick = setInterval(()=>{
    btn.textContent = `${label} ${Math.round((Date.now()-started)/1000)}s`;
  }, 1000);
  const ac = new AbortController();
  const killer = setTimeout(()=>ac.abort(), (secs||900)*1000);
  const poll = setInterval(loadHosts, 8000);   // watch the guest go down and come back
  try{
    const res = await fetch(url, {method:'POST', signal: ac.signal});
    const txt = await res.text();
    try { return JSON.parse(txt); }
    catch(e){ return {error:`server returned non-JSON (${res.status}): ${txt.slice(0,200)}`}; }
  } catch(e){
    return {error: e.name==='AbortError'
      ? `gave up after ${secs||900}s. The work may still be running — check the host card, `
        +`or run it from the CLI where you can watch it.`
      : `request failed: ${e.message}`};
  } finally {
    clearInterval(tick); clearTimeout(killer); clearInterval(poll);
    btn.disabled=false; btn.textContent=t; loadHosts();
  }
}

// Poll a job and render each stage as it is reported. The whole point is that a revert
// announces "rolling back", "starting", "still booting (45s)" rather than sitting silent.
async function follow(jid, id, btn, label){
  const started = Date.now();
  let seen = 0;
  while(true){
    await new Promise(r=>setTimeout(r, 1200));
    let j;
    try { j = await (await fetch(`/api/job/${jid}`)).json(); }
    catch(e){ return {error:`lost contact with the job: ${e.message}`}; }
    if(j.error) return {error:j.error};
    if(j.lines.length !== seen){
      seen = j.lines.length;
      box(id, `<pre>` + j.lines.map(l=>`  ${String(l.t).padStart(4)}s  ${esc(l.m)}`).join('\n') + `</pre>`);
    }
    btn.textContent = `${label} ${Math.round((Date.now()-started)/1000)}s`;
    if(j.done) return j.result || {};
    if((Date.now()-started)/1000 > 1800) return {error:'still running after 30 min; check the CLI'};
  }
}

async function run(id, btn){
  const card = btn.closest('.card');
  const destructive = card && card.dataset.baseline && card.dataset.baseline !== 'none';
  if(destructive && !confirm(
      `This reverts ${card.dataset.target} to the "${card.dataset.baseline}" baseline first.\n\n`
      + `Anything currently on that guest is discarded — including an installed sensor if the `
      + `baseline is "bare". Continue?`)) return;
  btn.disabled=true;
  const t = btn.textContent;
  const poll = setInterval(loadHosts, 8000);
  let r;
  try{
    const start = await (await fetch(`/api/run/${id}`, {method:'POST'})).json();
    r = start.error ? start : await follow(start.job, id, btn, 'working');
  } catch(e){ r = {error:`could not start: ${e.message}`}; }
  finally { clearInterval(poll); btn.disabled=false; btn.textContent=t; loadHosts(); }
  if(r.error){ box(id, `<pre class="bad">${esc(r.error)}</pre>`); return; }
  let out = '';
  if(r.prepared) out += `baseline -> ${r.prepared.snapshot} (${r.prepared.ready?'ready':'NOT READY'})\n\n`;
  else if(r.target === 'console') out += 'console scenario: nothing to revert\n\n';
  (r.steps||[]).forEach(s=>{
    out += `[${s.n}] ${s.name}  ${s.ok===true?'ok':s.ok===false?'FAIL':'--'}\n`;
    if(s.out) out += '     ' + s.out.split('\n').slice(0,4).join('\n     ') + '\n';
  });
  if(r.instructions) out += (out?'\n':'') + r.instructions;
  if(r.expect && r.expect.console) out += `\n\nLook in Falcon: ${r.expect.console.trim()}`;
  box(id, `<pre>${esc(out)}</pre>`);
  loadHosts();
}

async function grade(id, btn){
  btn.disabled=true;
  const r = await call(`/api/grade/${id}`, btn, 'checking', 300);
  if(r.error){ box(id, `<pre class="bad">${esc(r.error)}</pre>`); return; }
  const label = r.passed===true ? ['PASS','ok'] : r.passed===false ? ['NOT YET','bad']
                                                                  : ['UNVERIFIED','dim'];
  let h = `<div class="checks">`;
  for(const c of (r.checks||[])){
    const [m, cls] = MARK[String(c.ok)] || MARK['null'];
    h += `<div class="chk"><span class="m ${cls}">${m}</span>
            <span>${esc(c.label)}</span>
            <span class="why">${esc(c.reason||'')}</span></div>`;
    if((c.items||[]).length){
      h += `<div class="attest">` +
           c.items.map(i=>`<div>&#9744; ${esc(i)}</div>`).join('') + `</div>`;
    }
  }
  h += `<div class="verdict ${label[1]}">${label[0]}</div></div>`;
  if(r.passed !== true && r.hint) h += `<pre class="dim">${esc(r.hint.trim())}</pre>`;
  box(id, h);
  loadHosts();
}

loadHosts(); loadScen();
setInterval(loadHosts, 30000);
</script>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(PAGE)
