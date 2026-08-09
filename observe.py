#!/usr/bin/env python
"""Watch a browser session a human is driving. Read-only.

Not in the guide — the guide's agent drives a browser nobody else is using. This is the
opposite: attach to a tab a person is actively working in, and report what is there so
someone else can say "your time filter is set to the last hour".

The enabling fact: **a Chrome launched with --remote-debugging-port accepts CDP connections
while a human uses it normally.** Several clients can attach to the same tab at once. A
separate profile is not needed and does not help; what is needed is the debug flag at launch.

  ./observe.py --list
  ./observe.py --match falcon --once --shot /tmp/what-they-see.png
  ./observe.py --match falcon --watch --network

For a remote browser, tunnel first and set CDP_PORT (see README):
  ssh -N -L 9333:127.0.0.1:9222 <host>
  CDP_PORT=9333 ./observe.py --list

## What "read-only" means here

`ReadOnlyClient` refuses the CDP methods that act on the page: the whole `Input` domain,
navigation, reload, target create/close/activate, dialog handling, storage clearing. That is
structural — a typo or a future edit cannot quietly start clicking things in someone's live
console.

It does NOT block `Runtime.evaluate`, because rendering the page requires it. The honest
guarantee is narrower and worth stating plainly: this tool never dispatches input, and never
runs model-authored JavaScript — only the fixed expressions shipped in this repo. The page
snapshot is additionally taken with `mark=False`, so unlike the agent it does not tag elements
with attributes; the observed DOM is left exactly as the human's browser rendered it.
"""
import argparse
import asyncio
import base64
import os
import re
import sys
import time
from urllib.parse import parse_qsl, urlparse

from agent import cdp, snapshot, verify

# CDP methods that would act on a session someone else is driving.
_FORBIDDEN = re.compile(
    r"^(Input\.|Emulation\.|Storage\.clear|Browser\.close|Page\.(navigate|reload|close|"
    r"handleJavaScriptDialog|setDocumentContent)|Target\.(createTarget|closeTarget|"
    r"activateTarget|attachToTarget))"
)

# §13: never let these reach a prompt, a log, or a transcript. A Falcon console carries a
# bearer token on every request, and a naive network trace scoops it up.
_SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "x-csrf-token",
                      "proxy-authorization", "x-api-key", "x-auth-token"}
_SENSITIVE_QUERY = re.compile(r"(token|secret|key|auth|password|session|sig)", re.I)

_KEEP_HEADERS = ("content-type", "referer", "x-requested-with", "accept", "x-total-count")


class ReadOnlyClient(cdp.Client):
    """A CDP client that structurally cannot act on the page."""

    async def send(self, method, params=None):
        if _FORBIDDEN.match(method or ""):
            raise PermissionError(f"observe.py is read-only; refused CDP method {method!r}")
        return await super().send(method, params)


def redact_url(url):
    """Strip credential-ish query values but keep the parameters that explain a query —
    a Falcon filter lives in the query string and is the whole point of watching."""
    try:
        u = urlparse(url)
        if not u.query:
            return url[:300]
        parts = []
        for k, v in parse_qsl(u.query, keep_blank_values=True):
            parts.append(f"{k}=[redacted]" if _SENSITIVE_QUERY.search(k) else f"{k}={v}")
        return f"{u.scheme}://{u.netloc}{u.path}?{'&'.join(parts)}"[:300]
    except Exception:
        return (url or "")[:300]


def redact_headers(headers):
    out = {}
    for k, v in (headers or {}).items():
        lk = k.lower()
        if lk in _SENSITIVE_HEADERS:
            out[k] = "[redacted]"
        elif lk in _KEEP_HEADERS:
            out[k] = str(v)[:120]
    return out


# Response bodies carry credentials too, and more variably than headers do. A session
# endpoint returns a token in JSON; a docs page does not. Redact by key name rather than
# trying to recognise the value.
_SECRET_KEY = re.compile(
    r'("(?:[a-z_]*(?:token|secret|password|passwd|api[_-]?key|credential|session[_-]?id)'
    r'[a-z_]*)"\s*:\s*)"[^"]*"', re.I)
_BEARER = re.compile(r'\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{12,}', re.I)


def redact_body(text):
    if not text:
        return text
    text = _SECRET_KEY.sub(r'\1"[redacted]"', text)
    return _BEARER.sub(r"\1 [redacted]", text)


class NetworkRecorder:
    """Requests the page makes, with credentials stripped.

    For a single-page console this is usually more diagnostic than the DOM: the filters that
    decide what you see — time range, host group, severity — travel in the request, and are
    often not visible on screen at all.

    With `fetch_bodies()` it goes further and reads the responses. That matters because a
    console is an SPA talking to its own REST API: what the page renders is a view of JSON
    the browser already received, and reading that JSON is far more reliable than parsing
    the DOM it was turned into. It is also sometimes the only way in — CrowdStrike's docs
    portal renders its article body inside an iframe that leaves `document.body` with 20
    characters of text, while the content sits in plain sight in an API response.
    """

    def __init__(self):
        self.reqs = {}
        self.order = []
        self.finished = []

    def attach(self, client):
        def on_req(ev, *_):
            try:
                rid = ev.get("requestId")
                rq = ev.get("request", {})
                self.reqs[rid] = {
                    "method": rq.get("method"),
                    "url": redact_url(rq.get("url", "")),
                    "type": ev.get("type", "?"),
                    "headers": redact_headers(rq.get("headers")),
                    "body": (rq.get("postData") or "")[:400],
                    "status": None,
                }
                self.order.append(rid)
            except Exception:
                pass

        def on_res(ev, *_):
            try:
                rid = ev.get("requestId")
                if rid in self.reqs:
                    self.reqs[rid]["status"] = ev.get("response", {}).get("status")
            except Exception:
                pass

        def on_fin(ev, *_):
            try:
                rid = ev.get("requestId")
                if rid in self.reqs:
                    self.finished.append(rid)
            except Exception:
                pass

        client.register.Network.requestWillBeSent(on_req)
        client.register.Network.responseReceived(on_res)
        client.register.Network.loadingFinished(on_fin)

    async def fetch_bodies(self, client, match=None, max_bytes=400_000, limit=40):
        """Pull response bodies for finished requests.

        §13's rule: bodies are only available AFTER loadingFinished, and Chrome evicts them
        from its buffer, so call this promptly rather than at the end of a long session.
        `match` is a substring filter on the URL — without it you fetch every image and font
        on the page for nothing.
        """
        got = 0
        for rid in list(self.finished):
            if got >= limit:
                break
            r = self.reqs.get(rid)
            if not r or r.get("body_text") is not None:
                continue
            if match and match not in r.get("url", ""):
                continue
            try:
                res = await client.send("Network.getResponseBody", {"requestId": rid})
            except Exception:
                continue
            body = res.get("body") or res.get("result", {}).get("body", "")
            if res.get("base64Encoded") or res.get("result", {}).get("base64Encoded"):
                try:
                    body = base64.b64decode(body).decode("utf-8", "replace")
                except Exception:
                    continue
            r["body_text"] = redact_body(body)[:max_bytes]
            got += 1
        return got

    def bodies(self, match=None, min_len=0):
        """Captured responses, newest last."""
        out = []
        for rid in self.order:
            r = self.reqs.get(rid) or {}
            b = r.get("body_text")
            if b is None or len(b) < min_len:
                continue
            if match and match not in r.get("url", ""):
                continue
            out.append({"url": r["url"], "status": r.get("status"), "body": b})
        return out

    def drain(self, only_api=True):
        """Return and clear what has accumulated. XHR/fetch only by default — images, fonts
        and stylesheets are noise when the question is 'what did the UI ask for?'."""
        rows = []
        for rid in self.order:
            r = self.reqs.get(rid)
            if not r:
                continue
            if only_api and r.get("type") not in ("XHR", "Fetch"):
                continue
            rows.append(r)
        self.reqs, self.order = {}, []
        return rows


def list_tabs():
    return [t for t in cdp.targets() if t.get("type") == "page"]


def pick(tabs, match):
    if not match:
        return tabs[0] if tabs else None
    m = match.lower()
    for t in tabs:
        if m in (t.get("url", "") + " " + t.get("title", "")).lower():
            return t
    return None


async def report(client, shot=None, net=None, show_tree=True):
    await snapshot.settle(client, max_ms=1500)
    # mark=False: never tag elements in a DOM someone else is looking at.
    env = await snapshot.build(client, mark=False)
    print(f"\n{'=' * 72}")
    print(f"  {time.strftime('%H:%M:%S')}  {env.get('title', '')}")
    print(f"  {env.get('url', '')}")
    print(f"  {env.get('stats', {})}")
    if show_tree:
        print(f"{'-' * 72}")
        print(snapshot.render(env))
    if net is not None:
        rows = net.drain()
        if rows:
            print(f"{'-' * 72}\n  API CALLS (credentials redacted):")
            for r in rows[-12:]:
                print(f"   {r['method']:5} {r['status'] or '...'}  {r['url']}")
                if r["body"]:
                    print(f"         body: {r['body'][:200]}")
    if shot:
        await cdp.screenshot(client, shot)
        print(f"{'-' * 72}\n  screenshot -> {shot} ({os.path.getsize(shot)} bytes)")
    return env


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="list attachable tabs and exit")
    ap.add_argument("--match", help="substring of the tab's URL or title")
    ap.add_argument("--once", action="store_true", help="one report, then exit")
    ap.add_argument("--watch", action="store_true", help="keep reporting as the page changes")
    ap.add_argument("--interval", type=float, default=3.0, help="seconds between checks")
    ap.add_argument("--network", action="store_true", help="also record API calls")
    ap.add_argument("--shot", help="write a screenshot to this path each report")
    ap.add_argument("--no-tree", action="store_true", help="omit the page tree")
    a = ap.parse_args()

    tabs = list_tabs()
    if a.list or not (a.once or a.watch):
        if not tabs:
            print("no attachable tabs. Is Chrome running with --remote-debugging-port?")
            return 1
        print(f"{len(tabs)} tab(s) on CDP port {cdp.CDP_PORT}:\n")
        for t in tabs:
            print(f"  {t.get('title', '')[:60]}\n    {t.get('url', '')[:100]}\n")
        return 0

    t = pick(tabs, a.match)
    if not t:
        print(f"no tab matching {a.match!r}. Try --list.", file=sys.stderr)
        return 2
    print(f"observing (read-only): {t.get('title', '')}\n  {t.get('url', '')}")

    async with ReadOnlyClient(t["webSocketDebuggerUrl"]) as c:
        await asyncio.sleep(0.3)
        net = None
        if a.network:
            net = NetworkRecorder()
            net.attach(c)
            await c.send("Network.enable", {})

        await report(c, a.shot, net, not a.no_tree)
        if a.once:
            return 0

        last = await verify.capture(c)
        print(f"\nwatching every {a.interval}s — Ctrl-C to stop")
        try:
            while True:
                await asyncio.sleep(a.interval)
                now = await verify.capture(c)
                moved = (now.get("url") != last.get("url")
                         or now.get("head") != last.get("head")
                         or now.get("forms") != last.get("forms"))
                if moved:
                    await report(c, a.shot, net, not a.no_tree)
                    last = await verify.capture(c)
        except KeyboardInterrupt:
            print("\nstopped.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
