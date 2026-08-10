"""Reading Falcon console state.

This is the seam. Everything that talks to the console goes through here, so the eventual
decision about how `falcon-lab` depends on the agent — vendor the modules, or take the agent
as a package — touches this file and nothing else.

## How it reads

Not by scraping the page. The console is a single-page app that renders a view of JSON it
already fetched, so the honest thing to read is the JSON. `observe.NetworkRecorder` captures
the API responses the browser receives, which is both more faithful than the DOM and far more
stable than a rendered layout.

That is the same mechanism that got the documentation out of a portal whose article body sat
in an iframe with twenty characters of visible text. It also means grading survives a UI
redesign that would break any selector-based approach.

## What it needs

A Chrome signed in to the console, reachable over CDP (`CDP_PORT`, typically an SSH tunnel to
the machine holding the session). It **reads only**: navigate and observe, never click. The
session expires, so a check that cannot find its data says so rather than reporting a
failure — "I could not look" and "I looked and it was wrong" are different answers and the
grader must not conflate them.
"""
import asyncio
import os
import sys

# The agent modules live one level up until the repo split; this import is the whole
# dependency surface between the lab and the agent.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import cdp, snapshot  # noqa: E402
from observe import NetworkRecorder  # noqa: E402

CONSOLE = os.environ.get("FALCON_CONSOLE", "https://falcon.us-2.crowdstrike.com")


class ConsoleUnavailable(RuntimeError):
    """No usable console session. Distinct from a failed assertion on purpose."""


def url_for(path):
    if path.startswith("http"):
        return path
    return CONSOLE.rstrip("/") + "/" + path.lstrip("/")


async def _read(path, api_match, settle=14, max_wait=40):
    """Navigate to a console page and return the API responses it fetched."""
    tid, ws = await cdp.open_url("about:blank")
    try:
        async with cdp.Client(ws) as c:
            await asyncio.sleep(0.5)
            net = NetworkRecorder()
            net.attach(c)
            await c.send("Network.enable", {})
            await c.send("Page.navigate", {"url": url_for(path)})

            waited, seen, stable = 0, 0, 0
            while waited < max_wait:
                await asyncio.sleep(2)
                waited += 2
                await net.fetch_bodies(c, match=api_match or "/api/", limit=60)
                now = len(net.bodies(match=api_match, min_len=2))
                stable = stable + 1 if now == seen else 0
                seen = now
                if waited >= settle and stable >= 2 and now:
                    break

            title = await cdp.evaluate(c, "document.title") or ""
            if "Login" in title or "/login" in (await cdp.evaluate(c, "location.href") or ""):
                raise ConsoleUnavailable(
                    "the console session has expired — sign in again in the browser "
                    f"({CONSOLE}) and re-run")
            return net.bodies(match=api_match, min_len=2), title
    finally:
        await cdp.close_target(tid)


def read(path, api_match, settle=14):
    """Sync wrapper. Returns (bodies, page_title)."""
    return asyncio.run(_read(path, api_match, settle))


def check(path, api_match, expect_contains=None, expect_absent=None, settle=14):
    """Assert on what the console's own API returned.

    Deliberately substring matching rather than a JSON path language: the console's internal
    API is undocumented and unversioned, so its response *shape* is the least stable thing
    about it. The presence of a host group's name or a policy's name in the payload is a far
    more durable assertion than the key path it happens to sit under this month.

    Returns {ok, reason, matched, bodies_seen}. `ok is None` means the check could not be
    performed — never treat that as a pass.
    """
    try:
        bodies, title = read(path, api_match, settle)
    except ConsoleUnavailable as e:
        return {"ok": None, "reason": str(e), "bodies_seen": 0}
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "reason": f"console read failed: {str(e)[:120]}", "bodies_seen": 0}

    if not bodies:
        return {"ok": None, "bodies_seen": 0,
                "reason": f"no API response matching {api_match!r} on {path} — "
                          f"the page may have changed, or the filter is wrong"}

    blob = "\n".join(b["body"] for b in bodies)
    detail = {"bodies_seen": len(bodies), "bytes": len(blob)}

    if expect_contains is not None:
        want = expect_contains if isinstance(expect_contains, list) else [expect_contains]
        missing = [w for w in want if w not in blob]
        if missing:
            return {"ok": False, "reason": f"not found in the console's response: "
                                           f"{', '.join(repr(m) for m in missing)}", **detail}
    if expect_absent is not None:
        bad = expect_absent if isinstance(expect_absent, list) else [expect_absent]
        present = [b for b in bad if b in blob]
        if present:
            return {"ok": False, "reason": f"should not be present but is: "
                                           f"{', '.join(repr(p) for p in present)}", **detail}
    return {"ok": True, "reason": "console state matches", **detail}


def available():
    """Is there a usable console session right now?"""
    try:
        r = check("/", "/api/", settle=6)
        return r["ok"] is not None or "expired" not in (r.get("reason") or "")
    except Exception:  # noqa: BLE001
        return False
