"""Layers 4-5 — intent macros and model-authored JS (guide §7, §8).

The design contract: a macro resolves its own target in-page, by visible text / label /
row context. The model never picks an index and never sequences steps. Index-picking
among duplicates is a hard grounding problem; naming the row ("the Gadget one") is a
natural-language problem — so the hard part moves into deterministic code.

Every macro returns a structured result with a CONFIDENCE SIGNAL (matched:N,
section_chars, ...). Silent success is the enemy (§7.10 M4).
"""
import json
import logging
import re
from urllib.parse import urlparse

from . import cdp

log = logging.getLogger("agent.skills")

# --- §7.1 click_in_section — the most valuable macro in the set -------------------------
_CLICK_IN_SECTION = r"""
(function(ctx, ctrl, doClick){
  ctx=(ctx||'').trim().toLowerCase(); ctrl=(ctrl||'').trim().toLowerCase();
  const csel='a,button,input,select,summary,[role=button],[role=link],[role=menuitem],'
    +'[role=option],[onclick],[tabindex]';
  let best=null;
  for(const c of document.querySelectorAll(csel)){
    const r=c.getBoundingClientRect(); if(!(r.width>0&&r.height>0)) continue;
    const nm=((c.getAttribute&&c.getAttribute('aria-label'))||c.value||c.innerText||c.textContent||'')
      .trim().toLowerCase();
    if(!nm.includes(ctrl)) continue;
    let a=c, container=null;
    while(a && a!==document.body){                       // climb to SMALLEST ancestor containing ctx
      if((a.innerText||'').trim().toLowerCase().includes(ctx)){ container=a; break; }
      a=a.parentElement;
    }
    if(container){
      const size=(container.innerText||'').length;
      if(!best || size<best.size) best={c, size, cname:nm};   // smallest wins = tightest match
    }
  }
  if(!best) return JSON.stringify({ok:false,error:'no control in section',ctx,ctrl});
  best.c.scrollIntoView({block:'center', behavior:'instant'});
  if(doClick) best.c.click();
  const r=best.c.getBoundingClientRect();
  return JSON.stringify({ok:true,clicked:best.cname.slice(0,50),section_chars:best.size,
    x:Math.round(r.left+r.width/2), y:Math.round(r.top+r.height/2),
    w:Math.round(r.width), h:Math.round(r.height), vw:innerWidth, vh:innerHeight});
})
"""

# --- §7.2 click_by_text — tightest-match preference -------------------------------------
_CLICK_BY_TEXT = r"""
(function(text, nth, doClick){
  text=(text||'').trim().toLowerCase();
  const sel='a,button,input,select,textarea,summary,[role=button],[role=link],[role=menuitem],'
    +'[role=tab],[role=option],[role=checkbox],[role=radio],[onclick],[tabindex]';
  const cand=[];
  for(const el of document.querySelectorAll(sel)){
    const r=el.getBoundingClientRect(); if(!(r.width>0&&r.height>0)) continue;
    const nm=((el.getAttribute&&el.getAttribute('aria-label'))||el.value||el.innerText||el.textContent||'')
      .trim().toLowerCase();
    if(nm.includes(text)) cand.push({el, len:nm.length});
  }
  if(!cand.length) return JSON.stringify({ok:false,error:'no match',text});
  cand.sort((a,b)=>a.len-b.len);   // shortest name = tightest. "Add" beats "Add to wishlist".
  const pick=cand[Math.min(nth||0,cand.length-1)].el;
  pick.scrollIntoView({block:'center', behavior:'instant'});
  if(doClick) pick.click();
  const r=pick.getBoundingClientRect();
  return JSON.stringify({ok:true,matched:cand.length,
    clicked:((pick.innerText||pick.value||'')+'').trim().slice(0,50),
    x:Math.round(r.left+r.width/2), y:Math.round(r.top+r.height/2),
    w:Math.round(r.width), h:Math.round(r.height), vw:innerWidth, vh:innerHeight});
})
"""

# --- §7.3 fill_labeled_input — multi-source label resolution ----------------------------
_FILL_LABELED = r"""
(function(label, value){
  label=(label||'').trim().toLowerCase();
  const SKIP=['radio','checkbox','button','submit','reset','file','hidden','image','range','color'];
  function nameOf(el){
    const parts=[];
    const al=el.getAttribute&&el.getAttribute('aria-label'); if(al)parts.push(al);
    const ph=el.getAttribute&&el.getAttribute('placeholder'); if(ph)parts.push(ph);
    if(el.id){ try{ const l=document.querySelector('label[for="'+CSS.escape(el.id)+'"]'); if(l)parts.push(l.innerText); }catch(e){} }
    const c=el.closest&&el.closest('label'); if(c)parts.push(c.innerText);
    if(el.name)parts.push(el.name);
    return parts.join(' ').trim().toLowerCase();
  }
  let target=null;
  for(const el of document.querySelectorAll('input,textarea,select')){
    if(el.tagName==='INPUT' && SKIP.includes((el.getAttribute('type')||'text').toLowerCase())) continue;
    const r=el.getBoundingClientRect(); if(!(r.width>0&&r.height>0)) continue;
    if(nameOf(el).includes(label)){ target=el; break; }
  }
  if(!target) return JSON.stringify({ok:false,error:'no labeled field',label});
  target.focus();
  if(target.tagName==='SELECT'){
    let set=false;
    for(const o of target.options){                         // tier 1+2: exact value or exact text
      if(((o.value||'')+'').toLowerCase()===(''+value).toLowerCase()
         || (o.text||'').trim().toLowerCase()===(''+value).toLowerCase()){ target.value=o.value; set=true; break; } }
    if(!set){ for(const o of target.options){               // tier 3: substring
      if((o.text||'').toLowerCase().includes((''+value).toLowerCase())){ target.value=o.value; set=true; break; } } }
    target.dispatchEvent(new Event('change',{bubbles:true}));
    return JSON.stringify({ok:set, field:nameOf(target), value:target.value});
  }
  const proto=(target instanceof HTMLTextAreaElement)?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
  const setter=Object.getOwnPropertyDescriptor(proto,'value');
  if(setter&&setter.set) setter.set.call(target, value); else target.value=value;
  target.dispatchEvent(new Event('input',{bubbles:true}));
  target.dispatchEvent(new Event('change',{bubbles:true}));
  // M4: never report a bare ok:true for a write that stored nothing.
  const wrote=(''+value);
  return JSON.stringify(wrote===''
    ? {ok:false, error:'refused to write an empty value', field:nameOf(target)}
    : {ok:true, field:nameOf(target), value:wrote.slice(0,60)});
})
"""

# --- §7 wait_for_text -------------------------------------------------------------------
_WAIT_FOR_TEXT = r"""
(function(text, maxMs){
  text=(text||'').toLowerCase();
  return new Promise(resolve=>{
    const t0=performance.now();
    (function poll(){
      if((document.body.innerText||'').toLowerCase().includes(text))
        return resolve(JSON.stringify({found:true, waited:Math.round(performance.now()-t0)}));
      if(performance.now()-t0>=maxMs)
        return resolve(JSON.stringify({found:false, waited:Math.round(performance.now()-t0), text}));
      setTimeout(poll, 100);
    })();
  });
})
"""


async def _locate_then_click(client, js, args):
    """Resolve the target in-page, then dispatch a TRUSTED mouse event at its centre.

    Same hybrid as snapshot.click(): in-page targeting is robust and needs no coordinate
    math, but the click itself must be a real event or frameworks and anti-automation
    checks ignore it. Falls back to an in-page click when the box is unusable (off-screen
    or zero-size), by re-running the macro with doClick=true.
    """
    r = await cdp.eval_json(client, js + f"({args}, false)")
    if not r.get("ok"):
        return r
    x, y, w, h = r.get("x", 0), r.get("y", 0), r.get("w", 0), r.get("h", 0)
    if w > 0 and h > 0 and 0 <= x < r.get("vw", 0) and 0 <= y < r.get("vh", 0):
        await cdp.trusted_click(client, x, y)
        r["trusted"] = True
    else:
        r = await cdp.eval_json(client, js + f"({args}, true)")
        r["trusted"] = False
    for k in ("x", "y", "w", "h", "vw", "vh"):
        r.pop(k, None)                      # coordinates are harness detail, not model signal
    return r


async def click_in_section(client, section, control):
    return await _locate_then_click(
        client, _CLICK_IN_SECTION, f"{json.dumps(section)}, {json.dumps(control)}")


async def click_by_text(client, text, nth=0):
    return await _locate_then_click(
        client, _CLICK_BY_TEXT, f"{json.dumps(text)}, {json.dumps(nth)}")


async def fill_labeled_input(client, label, value):
    return await cdp.eval_json(
        client, _FILL_LABELED + f"({json.dumps(label)}, {json.dumps(value)})")


async def wait_for_text(client, text, ms=4000):
    return await cdp.eval_json(
        client, _WAIT_FOR_TEXT + f"({json.dumps(text)}, {json.dumps(ms)})", await_promise=True)


# --- §8 model-authored JS, with the §17 mitigation #3 guard -----------------------------
# eval_js is arbitrary code execution in an authenticated browser session, and the
# expression is authored by a model whose prompt contains untrusted page text. Deny the
# exfiltration primitives outright and log every expression that runs.
_DENY = re.compile(
    r"\b(fetch|XMLHttpRequest|WebSocket|EventSource|importScripts|navigator\s*\.\s*sendBeacon)\b"
    r"|document\s*\.\s*cookie"
    r"|\bimport\s*\("
    r"|\b(localStorage|sessionStorage|indexedDB)\b",
    re.I,
)


def _wrap(expr):
    """Wrap a model EXPRESSION into an async IIFE returning JSON {ok, value} | {ok:false, error}.

    Expression-only contract: multi-statement code must be written as an arrow IIFE
    `(()=>{ const x=...; return x; })()`, which is itself an expression — so there is
    exactly one code path and we never guess whether we got a statement or an expression.
    """
    return ("(async()=>{try{"
            "const __v=await (" + expr + ");"
            "return JSON.stringify({ok:true,value:(typeof __v==='undefined'?null:__v)});"
            "}catch(e){return JSON.stringify({ok:false,error:String((e&&e.message)||e)});}})()")


async def eval_js(client, expr):
    expr = expr or ""
    log.info("eval_js: %s", expr[:300])          # §17: log every expression executed
    hit = _DENY.search(expr)
    if hit:
        log.warning("eval_js BLOCKED (%s): %s", hit.group(0), expr[:200])
        return {"ok": False, "error": f"BLOCKED: '{hit.group(0)}' is not permitted in eval_js"}
    return await cdp.eval_json(client, _wrap(expr), await_promise=True)


async def extract_jsonld(client):
    """A dedicated op even though eval_js can do it — a NAMED op is far more reliably
    invoked than an equivalent expression the model must compose (§8)."""
    return await eval_js(
        client,
        "Array.from(document.querySelectorAll('script[type=\"application/ld+json\"]'))"
        ".map(s=>{try{return JSON.parse(s.textContent);}catch(e){return null;}})"
        ".filter(Boolean)")


# --- §17 mitigation #2: navigation allowlist -------------------------------------------
def navigation_allowed(url, allowlist):
    """Relative/hash routes stay on the current page and are always fine. Absolute URLs
    must match a host on the per-task allowlist.

    FAILS CLOSED. An empty or absent allowlist denies every absolute URL — it does not
    wave them through. A default-open allowlist is not a mitigation, and the model has
    already been observed obeying an injected `navigate` on the very first step.
    """
    if not url:
        return False, "empty url"
    if url.startswith("#") or url.startswith("/") or not re.match(r"^[a-zA-Z][\w+.-]*:", url):
        return True, ""
    p = urlparse(url)
    if p.scheme == "file":
        return (True, "") if allowlist and "file" in {a.lower() for a in allowlist} \
            else (False, "file:// navigation not permitted for this task")
    if p.scheme not in ("http", "https"):
        return False, f"scheme '{p.scheme}' not permitted"
    if not allowlist:
        return False, "no navigation allowlist is set for this task (absolute URLs denied)"
    host = (p.hostname or "").lower()
    for a in allowlist:
        a = a.lower()
        if host == a or host.endswith("." + a):
            return True, ""
    return False, f"host '{host}' not on the task allowlist {sorted(allowlist)}"
