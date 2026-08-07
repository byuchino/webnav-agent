"""Layers 1-3 — observation, settle, and acting by index (guide §4, §5, §6).

The observation layer is the single biggest lever in the whole guide: +6 on a 38-task
suite, where every other augment combined produced +2 (inside noise).

The v4 design in one line: ONE indented, document-order tree where actionability and
context are co-located. v3 split the content block from the controls list and lost the
ability to tell three identical "Remove" buttons apart — a small model cannot do joins.
"""
import json
import secrets

from . import cdp

MAX_CONTROLS = 600        # hard cap on interactive elements
MAX_VISITED = 20000       # hard cap on DOM nodes walked
MAX_LINES = 1400          # hard cap on output lines
TIME_BUDGET_MS = 600      # in-page wall-clock deadline
MAX_RENDER_CHARS = 7000   # cap on the text handed to the model

SETTLE_QUIET_MS = 400
SETTLE_MAX_MS = 3000

_BUILD_TMPL = r"""
(() => {
  const ATTR = "__ATTR__";
  const MAXC = __MAXCONTROLS__, MAXV = __MAXVISITED__, MAXL = __MAXLINES__,
        DEADLINE = performance.now() + __BUDGET__;
  const lines = [], controls = [], errors = [];
  let idx = 0, visited = 0, truncated = false;

  const SAL = new Set(['A','BUTTON','INPUT','SELECT','TEXTAREA','SUMMARY','OPTION']);
  const IROLES = new Set(['button','link','tab','switch','checkbox','radio','option','menuitem',
    'textbox','combobox','slider','listbox','menuitemcheckbox','menuitemradio']);
  const SENSITIVE = /pass|pwd|secret|token|auth|cvv|cvc|card|cc-|ssn|sin|otp|pin/i;
  const BLOCKISH = 'div,section,article,table,ul,ol,dl,li,nav,header,footer,main,aside,form,p,h1,h2,h3,h4,h5,h6,iframe,figure,blockquote';
  const INTERACTIVE_SEL = 'a,button,input,select,textarea,summary,[onclick],[tabindex],'
    + '[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[role=switch],[role=menuitem],[role=option]';

  // ---- visibility: geometry AND computed style. Both are required. ----
  function vis(el){
    try {
      const r = el.getBoundingClientRect();
      if(!(r.width > 0 && r.height > 0)) return false;
      const st = el.ownerDocument.defaultView.getComputedStyle(el);
      return st.visibility !== 'hidden' && st.display !== 'none'
          && parseFloat(st.opacity || '1') > 0.05;
    } catch(e){ return false; }
  }

  // Bounded scan for a laid-out descendant. Only ever runs on zero-size elements, so the
  // cost is paid on wrappers, not on the whole tree.
  function anyVisibleDesc(el, cap){
    try {
      const kids = el.querySelectorAll('*'), n = Math.min(kids.length, cap || 60);
      for(let i = 0; i < n; i++){
        const r = kids[i].getBoundingClientRect();
        if(r.width > 0 && r.height > 0) return true;
      }
    } catch(e){}
    return false;
  }

  // ---- three-way visibility (NOT in the guide; required by real pages) ----
  // The guide prunes any subtree whose ancestor fails vis(). But `display:contents` is
  // specifically designed to remove the box while its children lay out normally, so its
  // rect is 0x0 and the guide drops the entire subtree. On finance.yahoo.com that pruned
  // the whole quote body: 125 nodes visited, 9 lines out, no price — on a fully hydrated
  // 25KB page. Zero-size-but-not-hidden must DESCEND without emitting, not prune.
  function visKind(el){
    try {
      const st = el.ownerDocument.defaultView.getComputedStyle(el);
      if(st.display === 'none' || st.visibility === 'hidden'
         || parseFloat(st.opacity || '1') <= 0.05) return 'hidden';
      const r = el.getBoundingClientRect();
      if(r.width > 0 && r.height > 0) return 'visible';
      if(st.display === 'contents' || anyVisibleDesc(el, 60)) return 'passthrough';
      return 'hidden';
    } catch(e){ return 'hidden'; }
  }

  function role(el){
    const ex = el.getAttribute && el.getAttribute('role'); if(ex) return ex;
    if(el.tagName === 'INPUT'){
      const t = (el.getAttribute('type')||'text').toLowerCase();
      return {checkbox:'checkbox', radio:'radio', button:'button', submit:'button',
              reset:'button', range:'slider', file:'file', search:'searchbox'}[t] || 'textbox';
    }
    return {A:'link', BUTTON:'button', SELECT:'combobox', TEXTAREA:'textbox',
            SUMMARY:'button'}[el.tagName] || '';
  }

  function salient(el){
    if(SAL.has(el.tagName)) return true;
    if(IROLES.has(el.getAttribute && el.getAttribute('role'))) return true;
    if(el.hasAttribute && (el.hasAttribute('onclick') || el.getAttribute('tabindex') !== null)) return true;
    return false;
  }

  // ---- accessible name, in spec-ish precedence order ----
  function accName(el){
    let n = el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('alt')
            || el.getAttribute('placeholder') || el.getAttribute('title'));
    if(!n && el.getAttribute && el.getAttribute('aria-labelledby')){
      const ref = el.ownerDocument.getElementById(el.getAttribute('aria-labelledby'));
      if(ref) n = ref.innerText;
    }
    if(!n && el.id){
      try { const l = el.ownerDocument.querySelector('label[for="'+CSS.escape(el.id)+'"]');
            if(l) n = l.innerText; } catch(e){}
    }
    if(!n){ const c = el.closest && el.closest('label'); if(c) n = c.innerText; }
    if(!n) n = (el.innerText || el.textContent || '');
    return (n||'').trim().replace(/\s+/g,' ').slice(0,80);
  }

  // ---- fingerprint: identity for stale-action detection (§6) ----
  function fpOf(el){
    const nm = ((el.getAttribute && el.getAttribute('aria-label')) || el.innerText
                || el.textContent || '').trim().replace(/\s+/g,' ').slice(0,40);
    return el.tagName.toLowerCase() + '|'
         + ((el.getAttribute && el.getAttribute('role')) || '') + '|' + nm;
  }

  // ---- ROW CONTEXT: the disambiguator (§4.5 O3).
  // MEMOIZED per container — the guide's §4.6 weakness #1: the reference clones a DOM
  // subtree per interactive element, which at 600 controls dominates the 600ms budget.
  const _rowCache = new WeakMap();
  function rowCtx(el){
    let a = el.parentElement, hops = 0, cont = null;
    while(a && hops < 5){
      const r = a.getAttribute && a.getAttribute('role');
      if(a.tagName === 'TR' || a.tagName === 'LI' || r === 'row' || r === 'listitem' || r === 'option'
         || (a.getAttribute && (a.getAttribute('data-product') || a.getAttribute('data-row')
             || a.getAttribute('data-id')))){ cont = a; break; }
      a = a.parentElement; hops++;
    }
    if(!cont) cont = el.parentElement;
    if(!cont) return '';
    if(_rowCache.has(cont)) return _rowCache.get(cont);
    let t = '';
    try { const cl = cont.cloneNode(true);
          cl.querySelectorAll(INTERACTIVE_SEL).forEach(n => n.remove());
          t = (cl.innerText || cl.textContent || ''); }
    catch(e){ t = (cont.innerText || ''); }
    const out = t.trim().replace(/\s+/g,' ').slice(0,48);
    _rowCache.set(cont, out);
    return out;
  }

  // ---- state: only what differs from default, plus REDACTION (§4.5 O6/O9) ----
  function state(el){
    const s = {}; const ac = el.getAttribute && el.getAttribute('aria-checked');
    if(('checked' in el) && (el.type === 'checkbox' || el.type === 'radio')) s.checked = el.checked;
    else if(ac != null) s.checked = (ac === 'true');
    const ae = el.getAttribute && el.getAttribute('aria-expanded');  if(ae != null) s.expanded = (ae === 'true');
    const as = el.getAttribute && el.getAttribute('aria-selected');
    if(as != null) s.selected = (as === 'true'); else if(el.tagName === 'OPTION') s.selected = el.selected;
    const ap = el.getAttribute && el.getAttribute('aria-pressed');   if(ap != null) s.pressed = (ap === 'true');
    const cu = el.getAttribute && el.getAttribute('aria-current');   if(cu) s.current = cu;
    if(el.disabled === true || (el.getAttribute && el.getAttribute('aria-disabled') === 'true')) s.disabled = true;
    if(('value' in el) && el.value != null && el.tagName !== 'OPTION'
       && el.type !== 'checkbox' && el.type !== 'radio'){   // value="on" is a checkbox default: noise
      const t = ((el.getAttribute && el.getAttribute('type')) || '').toLowerCase();
      const meta = [el.name, el.id, (el.getAttribute && el.getAttribute('autocomplete')) || ''].join(' ');
      if(t === 'password' || SENSITIVE.test(meta)){ if(''+el.value) s.value = '[redacted]'; }
      else { const v = ''+el.value; if(v) s.value = v.slice(0,60); }
    }
    return s;
  }
  function fmtState(s){
    const ks = Object.keys(s); if(!ks.length) return '';
    return ' {' + ks.map(k => k+'='+(s[k]===true?'true':s[k]===false?'false':s[k])).join(', ') + '}';
  }

  function pad(d){ return '  '.repeat(Math.min(d, 14)); }
  function emit(d, text){ if(lines.length < MAXL) lines.push(pad(d) + text); else truncated = true; }
  function hasInteractive(el){ try { return !!el.querySelector(INTERACTIVE_SEL); } catch(e){ return true; } }
  function isLeafText(el){
    const t = (el.innerText || '').trim(); if(!t) return false;
    try { return !el.querySelector(BLOCKISH) && !el.querySelector(INTERACTIVE_SEL); } catch(e){ return false; }
  }

  // ---- TABLE -> Markdown with rowspan/colspan resolved into a rectangular grid ----
  function tableMD(t){
    try {
      const grid = [], occ = {}; const rows = Array.from(t.rows);
      for(let r = 0; r < rows.length; r++){
        let c = 0; if(!grid[r]) grid[r] = [];
        for(const cell of Array.from(rows[r].cells)){
          while(occ[r+','+c]) c++;                       // skip cells already filled by a span
          const txt = (cell.innerText||'').trim().replace(/\s+/g,' ');
          const cs = cell.colSpan || 1, rs = cell.rowSpan || 1;
          for(let i = 0; i < rs; i++) for(let j = 0; j < cs; j++){
            if(!grid[r+i]) grid[r+i] = [];
            grid[r+i][c+j] = (i===0 && j===0) ? txt : '';  // value in the anchor cell only
            occ[(r+i)+','+(c+j)] = true;
          }
          c += cs;
        }
      }
      const ncol = Math.max(1, ...grid.map(g => g.length));
      const ln = row => '| ' + Array.from({length: ncol}, (_, i) => row[i] || '').join(' | ') + ' |';
      let md = [ln(grid[0] || []), '|' + Array(ncol).fill('---').join('|') + '|'];
      for(let r = 1; r < grid.length; r++) md.push(ln(grid[r]));
      return md;
    } catch(e){ return [(t.innerText||'').trim()]; }
  }

  function walk(node, depth, ctx){
    for(const el of (node.childNodes ? Array.from(node.childNodes) : [])){
      if(++visited > MAXV || performance.now() > DEADLINE){ truncated = true; return; }
      if(controls.length >= MAXC){ truncated = true; return; }
      try {
        if(el.nodeType === 3){
          const t = (el.nodeValue||'').trim().replace(/\s+/g,' ');
          if(t.length > 1) emit(depth, t);
          continue;
        }
        if(el.nodeType !== 1) continue;
        const tag = el.tagName;
        if(tag==='SCRIPT'||tag==='STYLE'||tag==='NOSCRIPT'||tag==='TEMPLATE') continue;
        const vk = visKind(el);
        if(vk === 'hidden') continue;
        if(vk === 'passthrough'){ walk(el, depth, ctx); continue; }  // descend, emit nothing

        // STATIC structured blocks -> render coherently, then SKIP the subtree (§4.5 O4)
        if(tag === 'TABLE' && !hasInteractive(el)){ tableMD(el).forEach(r => emit(depth, r)); continue; }
        if((tag === 'UL' || tag === 'OL') && !hasInteractive(el)){
          const items = Array.from(el.children).filter(x => x.tagName === 'LI');
          emit(depth, '(' + items.length + ' items)');    // COUNT FOR THE MODEL (§4.5 O7)
          items.forEach(li => emit(depth+1, '- ' + (li.innerText||'').trim().replace(/\s+/g,' ')));
          continue;
        }
        if(tag === 'DL' && !hasInteractive(el)){
          let term = null;
          for(const c of Array.from(el.children)){
            if(c.tagName === 'DT') term = (c.innerText||'').trim();
            else if(c.tagName === 'DD') emit(depth, (term||'?') + ': ' + (c.innerText||'').trim());
          }
          continue;
        }

        // INTERACTIVE -> inline marker IN PLACE (context preserved). Do NOT recurse.
        // GUARD (not in the guide): a DIV/SPAN/SECTION is "salient" merely by carrying
        // tabindex or onclick, and on real pages such a wrapper can span half the page.
        // Marking it as one control swallows its whole subtree — on Yahoo an entire market
        // summary collapsed into a single `[7] div "US Markets S&P 500 ..."` line. Only
        // treat a generic container as a control when it is small and holds nothing else
        // interactive; otherwise fall through and recurse into it.
        if(salient(el)){
          const generic = (tag === 'DIV' || tag === 'SPAN' || tag === 'SECTION');
          const swallows = generic && (hasInteractive(el) || (el.innerText||'').length > 200);
          if(swallows){ walk(el, depth, ctx); continue; }
          el.setAttribute(ATTR, idx);
          const nm = accName(el), r = role(el) || tag.toLowerCase();
          const rc = rowCtx(el);
          const rcTag = (rc && rc.toLowerCase() !== nm.toLowerCase()) ? ('  (in: ' + rc + ')') : '';
          emit(depth, '[' + idx + '] ' + r + (nm ? (' "' + nm + '"') : '')
                    + fmtState(state(el)) + rcTag + (ctx ? ('  @' + ctx) : ''));
          controls.push({i: idx, fp: fpOf(el), name: nm, role: r, state: state(el)});
          idx++;
          continue;
        }

        // leaf text block -> ONE joined line. Prevents "phrasing fractures" (§4.5 O5).
        if(isLeafText(el)){ emit(depth, (el.innerText||'').trim().replace(/[ \t]+/g,' ')); continue; }

        // boundaries -> mark AND pierce (§4.5 O8)
        if(el.shadowRoot){
          emit(depth, '|shadow:' + (el.id || tag.toLowerCase()) + '|');
          walk(el.shadowRoot, depth+1, ctx + (ctx?'>':'') + 'shadow:' + (el.id || tag.toLowerCase()));
        }
        if(tag === 'IFRAME'){
          let d = null; try { d = el.contentDocument; } catch(e){}
          emit(depth, '|iframe:' + (el.id||'?') + '|' + (d ? '' : ' [cross-origin]'));
          if(d && d.body) walk(d.body, depth+1, ctx + (ctx?'>':'') + 'iframe:' + (el.id||'?'));
          continue;
        }

        walk(el, depth, ctx);       // generic container: recurse WITHOUT extra indent
      } catch(e){ if(errors.length < 20) errors.push((el.tagName||'?') + ': ' + String(e).slice(0,80)); }
    }
  }

  const _t0 = performance.now();
  walk(document.body, 0, '');

  let structured = [];
  try { for(const sc of document.querySelectorAll('script[type="application/ld+json"]')){
          try { structured.push(JSON.parse(sc.textContent)); } catch(e){} } } catch(e){}

  return JSON.stringify({
    url: location.href, title: document.title,
    viewport: {w: innerWidth, h: innerHeight, dpr: devicePixelRatio, sx: scrollX, sy: scrollY},
    truncated, tree: lines.join('\n'), controls, structured, errors,
    stats: {visited, controls_n: controls.length, lines: lines.length,
            elapsed_ms: Math.round(performance.now() - _t0)}
  });
})()
"""


async def build(client, prev_tokens=()):
    """One round-trip. Returns an env dict; the action layer needs `token` and `_fp`.

    A fresh random attribute name per snapshot (§16.3) means indices from a stale snapshot
    cannot silently resolve against a newer page state — you get a clean NOT_FOUND instead.

    `prev_tokens` cleanup fixes the guide's §16.3 bug: the reference cleans using the NEW
    token, so it removes nothing and old data-snap-* attributes accumulate forever.
    """
    if prev_tokens:
        sel = ",".join(f"[data-snap-{t}]" for t in prev_tokens)
        try:
            await cdp.evaluate(
                client,
                f"document.querySelectorAll({json.dumps(sel)})"
                f".forEach(e=>[...e.attributes].filter(a=>a.name.startsWith('data-snap-'))"
                f".forEach(a=>e.removeAttribute(a.name)));'ok'",
            )
        except Exception:
            pass

    token = secrets.token_hex(4)
    js = (_BUILD_TMPL.replace("__ATTR__", "data-snap-" + token)
          .replace("__MAXCONTROLS__", str(MAX_CONTROLS))
          .replace("__MAXVISITED__", str(MAX_VISITED))
          .replace("__MAXLINES__", str(MAX_LINES))
          .replace("__BUDGET__", str(TIME_BUDGET_MS)))
    env = await cdp.eval_json(client, js)
    if not isinstance(env, dict):
        env = {}
    env["token"] = token
    env.setdefault("controls", [])
    env["_fp"] = {c["i"]: c.get("fp", "") for c in env["controls"]}
    return env


def _flatten_struct(obj, out, prefix=""):
    """JSON-LD -> flat `a.b.c: value` lines. A 4B reads flat lines far better than nested JSON."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not k.startswith("@"):                 # drop @context/@type noise
                _flatten_struct(v, out, f"{prefix}{k}.")
    elif isinstance(obj, list):
        for v in obj[:5]:
            _flatten_struct(v, out, prefix)
    else:
        out.append(f"{prefix.rstrip('.')}: {obj}")


# §17 mitigation #4: delimit untrusted content explicitly. Page text goes verbatim into the
# prompt and the model's output is executed, so the boundary must be unmistakable.
UNTRUSTED_OPEN = "<<<BEGIN_UNTRUSTED_PAGE_DATA>>>"
UNTRUSTED_CLOSE = "<<<END_UNTRUSTED_PAGE_DATA>>>"


def render(env, max_chars=MAX_RENDER_CHARS):
    if not isinstance(env, dict):
        return ""
    parts = []
    if env.get("truncated"):
        parts.append("[observation truncated — page exceeded budget]")   # TELL the model (§4.5 O10)
    tree = (env.get("tree") or "").strip()
    if tree:
        parts.append("PAGE (indented document order; `[n]` = a clickable/typable element "
                     "you act on by index):\n"
                     + UNTRUSTED_OPEN + "\n" + tree[:max_chars] + "\n" + UNTRUSTED_CLOSE)
    struct_lines = []
    for obj in env.get("structured", [])[:3]:
        _flatten_struct(obj, struct_lines)
    if struct_lines:
        parts.append("STRUCTURED DATA:\n" + "\n".join(struct_lines[:40]))
    return "\n\n".join(parts)


_SETTLE_TMPL = r"""
(function(quietMs, maxMs){
  return new Promise((resolve) => {
    const W = window;
    if(!W.__snapSettle){                      // install ONCE per page — idempotent by design
      const S = { inflight: 0, lastMut: performance.now() };
      W.__snapSettle = S;
      try {
        const of = W.fetch;
        if(of && !of.__snapPatched){
          const nf = function(){
            S.inflight++; S.lastMut = performance.now();
            return of.apply(this, arguments).finally(() => {
              S.inflight = Math.max(0, S.inflight - 1); S.lastMut = performance.now();
            });
          };
          nf.__snapPatched = true; W.fetch = nf;
        }
      } catch(e){}
      try {
        const XP = W.XMLHttpRequest && W.XMLHttpRequest.prototype;
        if(XP && !XP.__snapPatched){
          const os = XP.send;
          XP.send = function(){
            S.inflight++; S.lastMut = performance.now();
            this.addEventListener('loadend', () => {
              S.inflight = Math.max(0, S.inflight - 1); S.lastMut = performance.now();
            });
            return os.apply(this, arguments);
          };
          XP.__snapPatched = true;
        }
      } catch(e){}
      try {
        const mo = new MutationObserver(() => { S.lastMut = performance.now(); });
        mo.observe(document.documentElement || document.body || document,
          { childList:true, subtree:true, attributes:true, characterData:true });
      } catch(e){}
    }
    const S = W.__snapSettle;
    const start = performance.now();
    (function check(){
      const now = performance.now();
      const ready = document.readyState !== 'loading';
      const quiet = (now - S.lastMut) >= quietMs;
      const idle  = S.inflight <= 0;
      if(ready && quiet && idle)
        return resolve(JSON.stringify({settled:true, waited:Math.round(now-start), inflight:S.inflight}));
      if(now - start >= maxMs)
        return resolve(JSON.stringify({settled:false, waited:Math.round(now-start),
                                       inflight:S.inflight, ready, quiet}));
      setTimeout(check, 50);
    })();
  });
})
"""


async def settle(client, quiet_ms=SETTLE_QUIET_MS, max_ms=SETTLE_MAX_MS):
    """Wait for the page to go quiet. Measured at zero net score lift — build it for
    determinism, not for points. Always returns within max_ms and reports settled:false."""
    js = _SETTLE_TMPL + f"({json.dumps(quiet_ms)}, {json.dumps(max_ms)})"
    return await cdp.eval_json(client, js, await_promise=True)


_RESOLVE_TMPL = r"""
(function(idx, op, text, expectFp){
  const ATTR = "__ATTR__";
  function fpOf(el){
    const nm = ((el.getAttribute && el.getAttribute('aria-label')) || el.innerText
                || el.textContent || '').trim().replace(/\s+/g,' ').slice(0,40);
    return el.tagName.toLowerCase() + '|'
         + ((el.getAttribute && el.getAttribute('role')) || '') + '|' + nm;
  }
  function find(root){
    let el = root.querySelector('[' + ATTR + '="' + idx + '"]'); if(el) return el;
    for(const e of root.querySelectorAll('*')){
      if(e.shadowRoot){ const f = find(e.shadowRoot); if(f) return f; }
      if(e.tagName === 'IFRAME'){
        try { const d = e.contentDocument; if(d){ const f = find(d); if(f) return f; } } catch(_){}
      }
    }
    return null;
  }
  const el = find(document);
  if(!el) return JSON.stringify({error:'NOT_FOUND'});
  if(expectFp && fpOf(el) !== expectFp) return JSON.stringify({error:'STALE', got: fpOf(el)});

  if(op === 'text')  return JSON.stringify({text: (el.innerText || el.textContent || '').trim()});
  if(op === 'rect'){
    // behavior:'instant' matters: under CSS scroll-behavior:smooth the rect would still be
    // the PRE-scroll one, and we would dispatch a trusted click at stale coordinates.
    el.scrollIntoView({block:'center', behavior:'instant'});
    const r = el.getBoundingClientRect();
    return JSON.stringify({x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2),
                           w: Math.round(r.width), h: Math.round(r.height),
                           vw: innerWidth, vh: innerHeight, tag: el.tagName.toLowerCase()});
  }
  if(op === 'click'){ el.scrollIntoView({block:'center'}); el.click(); return JSON.stringify({ok:'clicked'}); }
  if(op === 'focus'){ el.focus(); return JSON.stringify({ok:'focused'}); }
  if(op === 'check'){
    el.scrollIntoView({block:'center'});
    if('checked' in el){ el.click(); return JSON.stringify({ok:'checked', checked: el.checked}); }
    el.click(); return JSON.stringify({ok:'clicked'});
  }
  if(op === 'setval'){
    // CRITICAL: use the NATIVE value setter, not el.value = . React and every framework with
    // a controlled input hooks the native setter's descriptor; a plain assignment silently reverts.
    const proto = (el instanceof HTMLTextAreaElement) ? HTMLTextAreaElement.prototype
                                                      : HTMLInputElement.prototype;
    if(el.tagName === 'SELECT'){
      let set = false;
      for(const o of el.options){
        if((''+o.value).toLowerCase() === (''+text).toLowerCase()
           || (o.text||'').trim().toLowerCase() === (''+text).toLowerCase()){ el.value = o.value; set = true; break; }
      }
      if(!set){ for(const o of el.options){
        if((o.text||'').toLowerCase().includes((''+text).toLowerCase())){ el.value = o.value; set = true; break; } } }
      el.dispatchEvent(new Event('change',{bubbles:true}));
      return JSON.stringify({ok: set ? 'set' : 'no_matching_option', value: el.value});
    }
    el.focus();   // so a following `submit` presses Enter in THIS field (§15.3)
    const setter = Object.getOwnPropertyDescriptor(proto, 'value');
    if(setter && setter.set) setter.set.call(el, text); else el.value = text;
    el.dispatchEvent(new Event('input',  {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
    return JSON.stringify({ok:'set', value: (''+text).slice(0,60)});
  }
  return JSON.stringify({error:'unknown op'});
})
"""


async def _act(client, env, idx, op, text=""):
    token = env.get("token", "")
    expect_fp = env.get("_fp", {}).get(idx, "")
    js = (_RESOLVE_TMPL.replace("__ATTR__", "data-snap-" + token)
          + f"({json.dumps(idx)}, {json.dumps(op)}, {json.dumps(text)}, {json.dumps(expect_fp)})")
    return await cdp.eval_json(client, js)


async def click(client, env, idx, trusted=True):
    """Click by index, preferring a TRUSTED mouse event (§3's recommended hybrid).

    Resolve the element in-page (robust, no coordinate math), read back its rect centre,
    then dispatch a real mouse event there. The guide warns that in-page `el.click()` is
    isTrusted:false and that some frameworks ignore it entirely — and that is exactly what
    happened on yahoo.com: the model clicked the correct search-submit button, got back
    {"ok":"clicked"}, and the page did not react.

    Falls back to the in-page click when the element has no usable on-screen box.
    """
    if trusted:
        r = await _act(client, env, idx, "rect")
        if r.get("error"):
            return r
        x, y, w, h = r.get("x", 0), r.get("y", 0), r.get("w", 0), r.get("h", 0)
        if w > 0 and h > 0 and 0 <= x < r.get("vw", 0) and 0 <= y < r.get("vh", 0):
            await cdp.trusted_click(client, x, y)
            return {"ok": "clicked", "trusted": True, "at": [x, y]}
    return await _act(client, env, idx, "click")


async def check(client, env, idx):
    return await _act(client, env, idx, "check")


async def set_value(client, env, idx, text):
    return await _act(client, env, idx, "setval", text)


async def get_text(client, env, idx):
    return (await _act(client, env, idx, "text")).get("text", "")
