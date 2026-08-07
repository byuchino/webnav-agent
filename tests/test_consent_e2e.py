"""End-to-end proof that the consent gate stops real side effects.

The control condition matters as much as the test: under AUTO the click must actually fire.
Otherwise a broken fixture would look exactly like a working gate.

Needs Chrome on :9222. No model — actions are dispatched directly.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import agent, cdp, consent, snapshot  # noqa: E402

FIX = "file://" + os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "account.html")

FAILURES = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got={got!r} want={want!r}")
        FAILURES.append(label)


async def side_effect(c):
    """Whatever the page recorded actually happening."""
    return await cdp.evaluate(c, "document.getElementById('log').textContent")


async def find(env, needle):
    for i, m in (env.get("_meta") or {}).items():
        if needle.lower() in (m.get("name") or "").lower():
            return i
    return None


async def scenario(action, policy):
    """Fresh tab each time; returns (tool_result, side_effect_text)."""
    tid, ws = await cdp.open_url(FIX)
    try:
        async with cdp.Client(ws) as c:
            await asyncio.sleep(0.4)
            await snapshot.settle(c)
            env = await snapshot.build(c)
            d = dict(action)
            if "_find" in d:
                d["index"] = await find(env, d.pop("_find"))
            res = await agent.act(c, d, env, allowlist=["file"], policy=policy,
                                  goal="test")
            return res, (await side_effect(c))
    finally:
        await cdp.close_target(tid)


async def main():
    print("index click on 'Delete account':")
    res, eff = await scenario({"op": "click", "_find": "Delete account"}, consent.DESTRUCTIVE)
    check("  blocked under destructive", "BLOCKED BY CONSENT GATE" in res, True)
    check("  no side effect fired", eff, "")

    res, eff = await scenario({"op": "click", "_find": "Delete account"}, consent.AUTO)
    check("  CONTROL: fires under auto", eff, "SIDE EFFECT: ACCOUNT DELETED")

    print("macro click_in_section on the Visa row (different code path):")
    res, eff = await scenario(
        {"op": "click_in_section", "section": "Visa ending 4242", "control": "Remove"},
        consent.DESTRUCTIVE)
    check("  blocked under destructive", "BLOCKED BY CONSENT GATE" in res, True)
    check("  no side effect fired", eff, "")

    res, eff = await scenario(
        {"op": "click_in_section", "section": "Visa ending 4242", "control": "Remove"},
        consent.AUTO)
    check("  CONTROL: fires under auto", eff, "SIDE EFFECT: payment method removed")

    print("readonly policy:")
    res, eff = await scenario({"op": "setval", "_find": "Display name", "text": "Mallory"},
                              consent.READONLY)
    check("  setval refused", "BLOCKED BY CONSENT GATE" in res, True)
    res, _ = await scenario({"op": "click", "_find": "Open documentation"}, consent.READONLY)
    check("  link still followable", "BLOCKED" not in res, True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("consent gate holds end-to-end")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
