"""The verification layer (guide §16.4) — plus answer provenance, which the guide omits.

§16.4 is the largest gap in the reference design: production infers an action's success from
the model having said something. Two halves are needed, and only the first is in the guide.

**Action verification** (§16.4 as written). After a mutating action, assert that the page
actually changed. The expectation is produced by the ACTING layer, never by the model — an
expectation the model wrote would just restate its own mistake.

**Answer provenance** (not in the guide). The guide's verifiers cannot catch a read that is
extracted correctly from the wrong thing. This agent once reported `3,626.00` as CrowdStrike's
share price; the real answer was 214.42. `text_present("3,626.00")` would have PASSED — the
number was genuinely on the page:

    [110] link "CRWD.MX  CrowdStrike Holdings, Inc.  3,626.00  +82.70 (+2.33%)"

...as the Mexican listing, in pesos. Presence was never the problem. So instead of asking *is
this value on the page*, ask **where did it come from, and was there more than one candidate**.
Surfacing the surrounding row makes `.MX` visible; a match count above one flags the ambiguity
that caused the error in the first place.
"""
import json
import re

from . import cdp

# How many occurrences to report before giving up counting.
MAX_HITS = 6


_STATE_JS = r"""
(() => {
  const t = (document.body.innerText || '');
  // Form values are NOT part of innerText, so a successful fill changes no text and no
  // control count. Without this a working setval verifies as "nothing happened".
  let vals = '';
  try {
    vals = Array.from(document.querySelectorAll('input,textarea,select'))
             .map(e => (e.value == null ? '' : ('' + e.value))).join('');
  } catch (e) {}
  let h = 0;
  for (let i = 0; i < vals.length; i++) { h = (h * 31 + vals.charCodeAt(i)) | 0; }
  return JSON.stringify({
    url: location.href,
    len: t.length,
    head: t.slice(0, 1500),
    controls: document.querySelectorAll('a,button,input,select,textarea').length,
    forms: h
  });
})()
"""


async def capture(client):
    """A cheap before-picture, taken by the harness immediately before it acts."""
    return await cdp.eval_json(client, _STATE_JS)


async def verify_action(client, op, before):
    """Did anything actually change? Returns {ok, changed, detail}.

    `ok: None` means "no verifier applies", which is reported honestly rather than being
    dressed up as success — an unverifiable action must not look like a verified one.
    """
    if not isinstance(before, dict) or not before.get("url"):
        return {"ok": None, "reason": "no before-state captured"}

    after = await capture(client)
    if not isinstance(after, dict) or not after.get("url"):
        return {"ok": None, "reason": "could not read after-state"}

    url_changed = after.get("url") != before.get("url")
    text_changed = after.get("head") != before.get("head") or after.get("len") != before.get("len")
    controls_changed = after.get("controls") != before.get("controls")
    forms_changed = after.get("forms") != before.get("forms")
    changed = bool(url_changed or text_changed or controls_changed or forms_changed)

    detail = {}
    if url_changed:
        detail["url"] = after.get("url", "")[:120]
    if text_changed:
        detail["text_delta"] = (after.get("len", 0) - before.get("len", 0))
    if controls_changed:
        detail["controls_delta"] = (after.get("controls", 0) - before.get("controls", 0))
    if forms_changed:
        detail["form_values_changed"] = True

    # A navigation that goes nowhere, or a click that moves nothing, is the exact failure the
    # untrusted-click bug produced for a whole session while reporting {"ok":"clicked"}.
    return {"ok": changed, "changed": changed, **detail}


def _variants(value):
    """A value may be written differently in the page than in the answer: '$1,234.50' vs
    '1234.5'. Compare on a few normalisations rather than demanding an exact string."""
    v = (value or "").strip()
    out = {v}
    stripped = re.sub(r"[$£€,\s]", "", v)
    out.add(stripped)
    # 214.40 -> 214.4, so a trailing-zero difference does not read as "not on the page"
    if re.fullmatch(r"-?\d+\.\d+", stripped):
        out.add(stripped.rstrip("0").rstrip("."))
    return {x for x in out if x}


_PROV_JS = r"""
(function(needles, maxHits){
  const hits = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  let node;
  while ((node = walker.nextNode())) {
    if (hits.length >= maxHits) break;
    const raw = (node.nodeValue || '');
    if (!raw.trim()) continue;
    const flat = raw.replace(/[$£€,\s]/g, '');
    let matched = null;
    for (const n of needles) {
      if (raw.indexOf(n) !== -1 || (n && flat.indexOf(n) !== -1)) { matched = n; break; }
    }
    if (!matched) continue;

    // Climb to something row-ish so the context identifies WHICH thing this value belongs to.
    let a = node.parentElement, hops = 0, cont = null;
    while (a && hops < 6) {
      const r = a.getAttribute && a.getAttribute('role');
      if (a.tagName === 'TR' || a.tagName === 'LI' || a.tagName === 'A'
          || r === 'row' || r === 'listitem'
          || (a.innerText || '').trim().length > 25) { cont = a; break; }
      a = a.parentElement; hops++;
    }
    if (!cont) cont = node.parentElement;
    const ctx = ((cont && (cont.innerText || cont.textContent)) || raw)
                  .trim().replace(/\s+/g, ' ').slice(0, 160);
    if (seen.has(ctx)) continue;      // same row reached by several text nodes
    seen.add(ctx);
    hits.push({ matched: matched, context: ctx });
  }

  // Form values live outside innerText, so a value the agent just typed would otherwise
  // read as "not on the page" and be rejected as invented.
  try {
    for (const el of document.querySelectorAll('input,textarea,select')) {
      if (hits.length >= maxHits) break;
      const v = (el.value == null) ? '' : ('' + el.value);
      if (!v) continue;
      const flat = v.replace(/[$£€,\s]/g, '');
      let matched = null;
      for (const n of needles) {
        if (v.indexOf(n) !== -1 || (n && flat.indexOf(n) !== -1)) { matched = n; break; }
      }
      if (!matched) continue;
      const label = (el.getAttribute('aria-label') || el.getAttribute('placeholder')
                     || el.name || el.id || el.tagName.toLowerCase());
      const ctx = 'field "' + label + '" = ' + v.slice(0, 60);
      if (seen.has(ctx)) continue;
      seen.add(ctx);
      hits.push({ matched: matched, context: ctx, source: 'form-value' });
    }
  } catch (e) {}

  return JSON.stringify({ found: hits.length, hits: hits });
})
"""


async def answer_provenance(client, value):
    """Where on the page does this answer come from, and was it unambiguous?

    Returns {found, hits:[{matched, context}]}. `found == 0` means the value is not on the
    page at all — the strongest possible signal that an answer was invented. `found > 1`
    means several distinct places could have produced it, which is the shape of the CRWD.MX
    error: a real number lifted from the wrong row.
    """
    value = (value or "").strip()
    if not value:
        return {"found": 0, "hits": [], "reason": "empty answer"}
    # Very short answers ("3", "on") match everywhere and the count says nothing useful.
    if len(re.sub(r"[^0-9A-Za-z]", "", value)) < 2:
        return {"found": None, "hits": [], "reason": "answer too short to locate reliably"}

    needles = sorted(_variants(value), key=len, reverse=True)
    js = _PROV_JS + f"({json.dumps(needles)}, {MAX_HITS})"
    r = await cdp.eval_json(client, js)
    if not isinstance(r, dict) or "found" not in r:
        return {"found": None, "hits": [], "reason": "provenance check failed"}
    return r


def describe(prov):
    """One line for the operator and for the model's next observation."""
    found = prov.get("found")
    if found is None:
        return f"answer provenance: unchecked ({prov.get('reason', 'n/a')})"
    if found == 0:
        if prov.get("derived"):
            return ("answer provenance: not directly on the page, but the answer was computed "
                    "(eval_js / extract_jsonld) — check the tool result it came from")
        return "answer provenance: NOT FOUND on the page — the value may be invented"
    hits = prov.get("hits", [])
    if found == 1:
        return f"answer provenance: 1 match — from: {hits[0]['context']}"
    joined = " | ".join(h["context"][:70] for h in hits[:3])
    return (f"answer provenance: AMBIGUOUS — {found} distinct places on this page contain "
            f"this value: {joined}")
