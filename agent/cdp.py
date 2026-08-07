"""Layer 0 — CDP transport (guide §3).

The harness owns this entirely. The model never sees a WebSocket, a target id, or a
protocol handshake — it only ever names an intent. Three primitives cover ~95% of use:
Runtime.evaluate, Input.dispatch*Event, and Page.navigate/captureScreenshot.
"""
import asyncio
import base64
import json
import urllib.request

from cdp_use.client import CDPClient

CDP_PORT = 9222


def targets():
    with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=10) as r:
        return json.loads(r.read())


def stable_target():
    """A page target we can talk to. Skip devtools:// pages — attaching to them wastes a turn."""
    pages = [t for t in targets() if t.get("type") == "page"]
    for t in pages:
        if "devtools" not in t.get("url", ""):
            return t["webSocketDebuggerUrl"]
    return pages[0]["webSocketDebuggerUrl"]


class Client:
    """Thin async wrapper over cdp_use's CDPClient (which exposes start/stop/send_raw)."""

    def __init__(self, ws_url):
        self.ws_url = ws_url
        self._c = None

    async def __aenter__(self):
        self._c = CDPClient(self.ws_url)
        await self._c.start()
        return self

    async def __aexit__(self, *exc):
        try:
            await self._c.stop()
        except Exception:
            pass
        return False

    async def send(self, method, params=None):
        return await self._c.send_raw(method, params or {})


async def open_url(url, attempts=4):
    """Open a NEW tab and return (target_id, ws_url).

    Create via an existing client, then reconnect to the new target — you cannot create
    and drive in the same session cleanly. Target creation is genuinely flaky (§16.5),
    so retry with backoff.
    """
    last = None
    for i in range(attempts):
        try:
            async with Client(stable_target()) as c:
                r = await c.send("Target.createTarget", {"url": url})
            tid = r.get("targetId") or r.get("result", {}).get("targetId")
            for _ in range(20):  # let the tab exist before we look it up
                await asyncio.sleep(0.25)
                hit = [t for t in targets() if t.get("id") == tid]
                if hit:
                    return tid, hit[0]["webSocketDebuggerUrl"]
            last = RuntimeError("target never appeared")
        except Exception as e:  # noqa: BLE001 — retrying is the whole point
            last = e
        await asyncio.sleep(0.5 * (i + 1))
    raise RuntimeError(f"open_url failed after {attempts} attempts: {last}")


async def close_target(tid):
    try:
        async with Client(stable_target()) as c:
            await c.send("Target.closeTarget", {"targetId": tid})
    except Exception:
        pass


def eval_value(r):
    """Unwrap Runtime.evaluate, SURFACING in-page exceptions instead of returning ''.

    Runtime.evaluate reports in-page exceptions in a side channel, not by raising. A macro
    that silently returns '' on a JS syntax error will cost you hours (§3).
    """
    if r.get("exceptionDetails"):
        ex = r["exceptionDetails"]
        desc = ex.get("exception", {}).get("description") or json.dumps(ex)
        raise RuntimeError("page JS error: " + desc[:300])
    return r.get("result", {}).get("value", "")


async def evaluate(client, expression, await_promise=False):
    r = await client.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
    )
    return eval_value(r)


async def eval_json(client, expression, await_promise=False):
    """Evaluate JS that returns a JSON string; parse it, never raise on bad JSON."""
    try:
        raw = await evaluate(client, expression, await_promise)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"unwrap_failed: {str(e)[:160]}"}


async def trusted_click(client, x, y):
    """A real click at viewport CSS coordinates — isTrusted:true.

    element.click() from in-page JS produces isTrusted:false, which many real sites and
    every anti-automation check treat differently; some frameworks ignore it entirely.
    """
    for ev, btns, cnt in (("mouseMoved", 0, 0), ("mousePressed", 1, 1), ("mouseReleased", 0, 1)):
        p = {"type": ev, "x": x, "y": y}
        if ev != "mouseMoved":
            p.update({"button": "left", "buttons": btns, "clickCount": cnt})
        await client.send("Input.dispatchMouseEvent", p)


_RECT_JS = r"""
(function(sel){
  const el = document.querySelector(sel);
  if(!el) return null;
  el.scrollIntoView({block:'center'});
  const r = el.getBoundingClientRect();
  return JSON.stringify({x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)});
})
"""


async def click_element_trusted(client, css_selector):
    """The recommended hybrid: resolve the element in-page (robust, no coordinate math),
    read back its rect centre, then dispatch a TRUSTED mouse event at those coordinates."""
    cell = await eval_json(client, _RECT_JS + f"({json.dumps(css_selector)})")
    if not cell:
        return {"ok": False, "error": "NOT_FOUND"}
    await trusted_click(client, cell["x"], cell["y"])
    return {"ok": True, **cell}


async def press_enter(client):
    """Real Enter key input. Submitting a search box by pressing Enter in the focused field
    is far more reliable than depending on a small model to pick the right submit button
    (§15.3) — and unlike an in-page form.submit() it fires the framework's key handlers."""
    base = {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13,
            "nativeVirtualKeyCode": 13}
    await client.send("Input.dispatchKeyEvent", {"type": "keyDown", "text": "\r", **base})
    await client.send("Input.dispatchKeyEvent", {"type": "keyUp", **base})
    return {"ok": True, "pressed": "Enter"}


async def focused_info(client):
    return await eval_json(client, """(()=>{const a=document.activeElement;
      return JSON.stringify({tag:a?a.tagName.toLowerCase():null,
        id:a&&a.id||null, name:a&&a.name||null,
        value:(a&&'value' in a)?(''+a.value).slice(0,60):null});})()""")


async def screenshot(client, path, clip=None, scale=1.0):
    """`clip`={x,y,w,h} crops to a region; `scale`>1 magnifies AT CAPTURE TIME (§12.3)."""
    params = {"format": "png", "captureBeyondViewport": False}
    if clip:
        params["clip"] = {
            "x": float(clip["x"]), "y": float(clip["y"]),
            "width": float(clip["w"]), "height": float(clip["h"]), "scale": float(scale),
        }
    r = await client.send("Page.captureScreenshot", params)
    data = r.get("data") or r.get("result", {}).get("data", "")
    with open(path, "wb") as f:
        f.write(base64.b64decode(data))
    return path
