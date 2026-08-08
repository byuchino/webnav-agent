"""Verification layer tests (guide §16.4 plus answer provenance).

The centrepiece is the CRWD.MX regression: the exact failure this agent produced against
finance.yahoo.com, reduced to a fixture. Note that the guide's own suggested verifier
(`text_present`) PASSES on the wrong answer, because the wrong number is genuinely on the
page. That is precisely why provenance exists.

Needs Chrome on :9222. No model — verification is deterministic.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import agent, cdp, consent, snapshot, verify  # noqa: E402

FIXDIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")
FAILURES = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got={got!r} want={want!r}")
        FAILURES.append(label)


def show(label, text):
    print(f"        {label}: {text}")


async def with_page(fixture, fn):
    tid, ws = await cdp.open_url("file://" + os.path.join(FIXDIR, fixture))
    try:
        async with cdp.Client(ws) as c:
            await asyncio.sleep(0.4)
            await snapshot.settle(c)
            return await fn(c)
    finally:
        await cdp.close_target(tid)


async def main():
    print("answer provenance — the CRWD.MX regression:")

    async def prov_cases(c):
        return {
            "right": await verify.answer_provenance(c, "214.42"),
            "wrong": await verify.answer_provenance(c, "3,626.00"),
            "wrong_norm": await verify.answer_provenance(c, "3626"),
            "invented": await verify.answer_provenance(c, "999.77"),
        }

    r = await with_page("quotes.html", prov_cases)

    check("correct answer is located", r["right"]["found"], 1)
    ctx_right = r["right"]["hits"][0]["context"]
    check("  ...and its context names NASDAQ/USD", "NASDAQ" in ctx_right, True)
    show("context", ctx_right)

    # The whole point: the wrong answer is FOUND (so text_present would pass), but its
    # provenance immediately exposes which listing it came from.
    check("wrong answer is present on the page", r["wrong"]["found"] >= 1, True)
    ctx_wrong = r["wrong"]["hits"][0]["context"]
    check("  ...but provenance exposes CRWD.MX", "CRWD.MX" in ctx_wrong, True)
    check("  ...and exposes the currency", "MXN" in ctx_wrong, True)
    show("context", ctx_wrong)

    check("normalised form still located", r["wrong_norm"]["found"] >= 1, True)
    check("invented value is NOT found", r["invented"]["found"], 0)
    show("describe(invented)", verify.describe(r["invented"]))

    print("action verification — a click that does nothing must not read as success:")

    async def noop_case(c):
        env = await snapshot.build(c)
        # Index 0 is a plain text row with no handler; clicking it changes nothing.
        before = await verify.capture(c)
        idx = next(iter(env.get("_meta") or {}), None)
        return before, idx, env

    async def real_case(c):
        env = await snapshot.build(c)
        before = await verify.capture(c)
        target = None
        for i, m in (env.get("_meta") or {}).items():
            if "Remove" in (m.get("name") or ""):
                target = i
                break
        await snapshot.click(c, env, target)
        return await verify.verify_action(c, "click", before)

    v_real = await with_page("cart.html", real_case)
    check("a real removal is verified as changed", v_real.get("ok"), True)
    show("detail", str({k: v for k, v in v_real.items() if k != "ok"}))

    async def nothing_happens(c):
        before = await verify.capture(c)
        return await verify.verify_action(c, "click", before)

    v_noop = await with_page("cart.html", nothing_happens)
    check("no action at all is verified as unchanged", v_noop.get("ok"), False)

    # Both of these were false signals introduced with the verifier itself: a successful fill
    # changes no page text and no control count, and a typed value never appears in innerText.
    # Together they cost three wasted steps per run before being fixed.
    print("form writes — the false-signal regressions:")

    async def fill_case(c):
        env = await snapshot.build(c)
        target = None
        for i, m in (env.get("_meta") or {}).items():
            if "you@example.com" in (m.get("name") or ""):
                target = i
                break
        before = await verify.capture(c)
        await snapshot.set_value(c, env, target, "alice@example.com")
        v = await verify.verify_action(c, "setval", before)
        p = await verify.answer_provenance(c, "alice@example.com")
        return v, p

    v_fill, p_fill = await with_page("cart.html", fill_case)
    check("a fill is verified as changed", v_fill.get("ok"), True)
    check("  ...via form values, not page text", v_fill.get("form_values_changed"), True)
    check("typed value is locatable", p_fill["found"] >= 1, True)
    check("  ...and attributed to the field", p_fill["hits"][0].get("source"), "form-value")
    show("context", p_fill["hits"][0]["context"])

    print("derived answers are not treated as invented:")
    computed = {"found": 0, "hits": [], "derived": True}
    check("describe() distinguishes computed", "may be invented" in verify.describe(computed), False)
    show("describe(derived)", verify.describe(computed))

    print("\nfull dispatch path attaches the verdict:")

    async def dispatch(c):
        env = await snapshot.build(c)
        target = None
        for i, m in (env.get("_meta") or {}).items():
            if "Remove" in (m.get("name") or ""):
                target = i
                break
        return await agent.act(c, {"op": "click", "index": target}, env,
                               allowlist=["file"], policy=consent.AUTO, goal="test")

    res = await with_page("cart.html", dispatch)
    check("TOOL RESULT carries verification", "verified" in res, True)
    show("result", res[:150])

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("verification layer holds")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
