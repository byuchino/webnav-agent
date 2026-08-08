"""The control loop and the op dispatcher (guide §15.1, Appendix A).

Observe -> decide -> act -> report, with the guide's ordering rule: the highest-signal,
most-recent information goes FIRST and the large, mostly-static page tree goes LAST,
because a small model attends most reliably to the top of a long observation.
"""
import asyncio
import json
import logging
from urllib.parse import urlparse

from . import cdp, consent, llm, skills, snapshot, verify

# Ops whose whole point is to change something. If one of these reports success and nothing
# on the page moved, that is the untrusted-click failure mode: {"ok":"clicked"} for an event
# the page ignored. Verify them (§16.4).
VERIFIED_OPS = {"click", "check", "setval", "submit", "navigate",
                "click_by_text", "click_in_section", "fill_labeled_input"}

log = logging.getLogger("agent")


# `report` is terminal: the control loop consumes it and breaks, so it never reaches the
# dispatcher. The guide's Appendix A version exempts it from `extra` but not from
# `missing`, which trips on any menu that documents it — exempt it from both.
TERMINAL_OPS = {"report"}


def assert_menu_consistent(op_doc, handlers):
    """Appendix A watchdog. The reference implementation advertises `type_text` and
    `drag_to` in the production system prompt but has no dispatch branch for either —
    two elif-chains that drifted apart. Assert at startup instead."""
    missing = set(op_doc) - set(handlers) - TERMINAL_OPS
    extra = set(handlers) - set(op_doc) - TERMINAL_OPS
    assert not missing, f"documented but not dispatchable: {sorted(missing)}"
    assert not extra, f"dispatchable but undocumented: {sorted(extra)}"


def _value_of(d):
    """Source a text value from whichever key the model actually used.

    §14.5's schema-key signaling works — `exact_value_only_no_prose` really does suppress
    prose on `report` — but it backfires as a *flat* schema field: it is the most
    descriptively-named key on the menu, so the model reaches for it as "the place a value
    goes" on fill ops too. Observed: {"op":"fill_labeled_input","label":"Email",
    "exact_value_only_no_prose":"alice@example.com"}. Accept the whole family rather than
    silently filling the field with "".
    """
    for k in ("value", "text", "exact_value_only_no_prose"):
        v = d.get(k)
        if isinstance(v, str) and v != "":
            return v
    return None


async def act(c, d, env, allowlist=None, policy=consent.DESTRUCTIVE, allow_eval_js=True,
              goal=""):
    """Dispatch ONE action and return a TOOL RESULT string to surface on the next turn.

    §6's headline defect: the reference computes NOT_FOUND / STALE and then throws the
    result away, so a failed click is indistinguishable from a successful one to both the
    harness and the model. An action layer that cannot report failure is not an action
    layer; it is a random number generator. Every branch here returns its result.
    """
    op = d.get("op")
    i = d.get("index")
    url = env.get("url", "")

    # --- consent gate (§17 mitigation 6) ------------------------------------------------
    # Index ops know their target from the snapshot, so they are checked here, up front.
    # The macros resolve their own targets, so they carry the gate inward as a callback and
    # check after resolving but before clicking.
    meta = (env.get("_meta") or {}).get(i, {})

    async def _gate(name, role="", ctx="", text=""):
        return await consent.gate(policy, op, index=i, name=name, role=role, ctx=ctx,
                                  text=text, url=url, goal=goal)

    async def _macro_gate(resolved_name):
        # The section the model named is this macro's row context — "Remove" is judged very
        # differently in "Visa ending 4242" than in a list of search filters.
        return await consent.gate(policy, "click", name=resolved_name, role="button",
                                  ctx=d.get("section", ""), url=url, goal=goal)

    if op in consent.MUTATING and op not in ("click_by_text", "click_in_section"):
        ok, why = await _gate(meta.get("name", ""), meta.get("role", ""),
                              text=_value_of(d) if op == "setval" else "")
        if not ok:
            return f"TOOL RESULT {op}[{i}]: " + json.dumps(
                {"ok": False, "error": f"BLOCKED BY CONSENT GATE: {why}"})

    # Before-picture for §16.4. Taken by the harness, never by the model: an expectation the
    # model authored would only restate whatever it already believes.
    before = await verify.capture(c) if op in VERIFIED_OPS else None

    if op == "click":
        r = await snapshot.click(c, env, i)
    elif op == "check":
        r = await snapshot.check(c, env, i)
    elif op == "setval":
        v = _value_of(d)
        if v is None:
            r = {"ok": False, "error": "setval needs a non-empty \"text\" field"}
        else:
            r = await snapshot.set_value(c, env, i, v)
    elif op == "scroll":
        by = int(d.get("by", 400))
        r = await cdp.eval_json(
            c, f"(()=>{{window.scrollBy(0,{by});"
               f"return JSON.stringify({{ok:true,scrollY:window.scrollY}});}})()")
    elif op == "navigate":
        url = d.get("url", "")
        allowed, why = skills.navigation_allowed(url, allowlist)
        if not allowed:
            r = {"ok": False, "error": f"NAVIGATION BLOCKED: {why}"}
            log.warning("navigation blocked: %s (%s)", url, why)
        else:
            await c.send("Page.navigate", {"url": url})
            await asyncio.sleep(1.0)
            r = {"ok": True, "navigated": url}
    elif op == "submit":
        who = await cdp.focused_info(c)
        await cdp.press_enter(c)
        await asyncio.sleep(1.2)          # let a navigation/XHR start before we re-observe
        r = {"ok": True, "pressed": "Enter", "in_field": who}
    elif op == "wait":
        await asyncio.sleep(min(int(d.get("ms", 500)), 5000) / 1000)
        r = {"ok": True}
    elif op == "eval_js":
        if not allow_eval_js:
            r = {"ok": False, "error": "eval_js is disabled for this task"}
        else:
            r = await skills.eval_js(c, d.get("expr", ""))
    elif op == "extract_jsonld":
        r = await skills.extract_jsonld(c)          # fixed, read-only expression
    elif op == "click_by_text":
        r = await skills.click_by_text(c, d.get("text", ""), d.get("nth", 0), _macro_gate)
    elif op == "click_in_section":
        r = await skills.click_in_section(c, d.get("section", ""), d.get("control", ""),
                                          _macro_gate)
    elif op == "fill_labeled_input":
        v = _value_of(d)
        if v is None:
            r = {"ok": False, "error": "fill_labeled_input needs a non-empty \"value\" field"}
        else:
            r = await skills.fill_labeled_input(c, d.get("label", ""), v)
    elif op == "wait_for_text":
        r = await skills.wait_for_text(c, d.get("text", ""), int(d.get("ms", 4000)))
    else:
        r = {"ok": False, "error": f"unknown op '{op}'"}

    # Only verify actions that claimed to work. Re-checking an already-failed action adds a
    # round trip and tells us nothing we don't know.
    claimed = isinstance(r, dict) and r.get("ok") not in (False, None)
    if before is not None and claimed:
        v = await verify.verify_action(c, op, before)
        if v.get("ok") is False:
            r = dict(r)
            r["VERIFY"] = "the page did not change — this action appears to have had no effect"
            log.warning("verify: %s claimed success but nothing changed", op)
        elif v.get("ok"):
            r = dict(r)
            r["verified"] = {k: v[k] for k in ("url", "text_delta", "controls_delta") if k in v}

    return f"TOOL RESULT {op}" + (f"[{i}]" if i is not None else "") + ": " + json.dumps(r)


HANDLED = {"click", "check", "setval", "scroll", "navigate", "submit", "wait", "eval_js",
           "extract_jsonld", "click_by_text", "click_in_section", "fill_labeled_input",
           "wait_for_text"}
assert_menu_consistent(llm.OP_DOC, HANDLED)


# A stuck-loop watchdog. Not in the guide's budget table (§16.2), but observed burning an
# entire step budget: the model repeated one failing fill 8 times because nothing told it
# the repeat was futile. Per the guide's meta-rule, the budget must ANNOUNCE itself — a
# silent one just converts a stall into a wrong answer.
REPEAT_WARN = 2   # identical consecutive actions before we warn the model
REPEAT_STOP = 4   # ... before we stop and force a report


async def solve(c, goal_text, max_steps=8, allowlist=None, policy=consent.DESTRUCTIVE,
                allow_eval_js=True):
    hist = []
    last_tool = ""
    answer = None
    tokens = []
    prev_sig = None
    repeats = 0
    rechecked = False
    prov = None

    for step in range(max_steps):
        # --- OBSERVE (with a retry: a mid-navigation snapshot legitimately throws) ---
        try:
            await snapshot.settle(c)
            env = await snapshot.build(c, prev_tokens=tokens[-3:])
        except Exception as e:  # noqa: BLE001
            log.info("step %d: observe error (%s), re-settling", step, str(e)[:60])
            await asyncio.sleep(1.5)
            try:
                env = await snapshot.build(c)
            except Exception:
                continue
        if env.get("token"):
            tokens.append(env["token"])

        # --- ASSEMBLE: highest-signal first (tool result outranks the page) ---
        obs = snapshot.render(env)
        if last_tool:
            obs = last_tool + "\n\n" + obs
        if repeats >= REPEAT_WARN:
            obs = (f"WARNING: you have issued the same action {repeats} times and it has not "
                   f"changed the page. It is not working — do something DIFFERENT: use another "
                   f"op, correct the field names, or report the answer from what you can already "
                   f"see.\n\n") + obs

        # --- FORCED REPORT near the cap: never end with nothing ---
        g = goal_text + ("\n(Near the step limit — report your best answer now.)"
                         if step >= max_steps - 2 or repeats >= REPEAT_STOP - 1 else "")

        # --- DECIDE off-thread: urllib inference is blocking and would stall the event
        #     loop, which drops the CDP WebSocket (§15.1). ---
        d = await asyncio.to_thread(llm.decide, g, obs, hist)
        hist.append(d)
        log.info("step %d: %s", step, json.dumps(d)[:160])

        if "report" in d:
            answer = d["report"]
            prov = await verify.answer_provenance(c, str(answer))
            # An answer produced by eval_js or extract_jsonld is DERIVED — a computed total
            # is legitimately absent from the page, so "not found" is not evidence of
            # invention. Rejecting those was a false positive that cost three extra steps.
            derived = any(h.get("op") in ("eval_js", "extract_jsonld") for h in hist
                          if isinstance(h, dict))
            prov["derived"] = derived
            log.info("        %s", verify.describe(prov))
            if prov.get("found") == 0 and not derived and not rechecked:
                rechecked = True
                last_tool = (f"TOOL RESULT report: your answer {answer!r} does not appear "
                             f"anywhere on this page. Re-read the PAGE tree and report a "
                             f"value that is actually present.")
                log.warning("verify: reported value not on page, asking again")
                hist.append({"_rejected_report": str(answer)})
                answer = None
                continue
            break

        sig = json.dumps(d, sort_keys=True)
        repeats = repeats + 1 if sig == prev_sig else 0
        prev_sig = sig
        if repeats >= REPEAT_STOP:
            log.warning("stuck: identical action %d times, stopping early", repeats)
            break

        try:
            last_tool = await act(c, d, env, allowlist, policy, allow_eval_js,
                                  goal_text) or last_tool
        except Exception as e:  # noqa: BLE001
            last_tool = f"TOOL RESULT {d.get('op')}: ERROR {str(e)[:80]}"   # tell the model
        log.info("        %s", last_tool[:200])

    return answer, hist, prov


def default_allowlist(start_url):
    """The task's own origin is implicitly allowed; everything else must be named
    explicitly. This keeps same-site navigation working without opening the door."""
    p = urlparse(start_url)
    if p.scheme == "file":
        return ["file"]
    return [p.hostname.lower()] if p.hostname else []


async def run(goal, start_url, max_steps=8, allowlist=None, policy=consent.DESTRUCTIVE,
              allow_eval_js=True):
    allowlist = list(allowlist) if allowlist else default_allowlist(start_url)
    log.info("navigation allowlist: %s  |  consent policy: %s  |  eval_js: %s",
             allowlist, policy, "on" if allow_eval_js else "off")
    tid, ws = await cdp.open_url(start_url)
    try:
        async with cdp.Client(ws) as c:
            await asyncio.sleep(0.5)
            return await solve(c, goal, max_steps, allowlist, policy, allow_eval_js)
    finally:
        await cdp.close_target(tid)
