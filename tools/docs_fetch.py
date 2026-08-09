#!/usr/bin/env python3
"""Harvest CrowdStrike documentation through an authenticated browser session.

  ./tools/docs_fetch.py chapter https://docs.crowdstrike.com/r/en-US/g6auvcg3/f8a0f751
  ./tools/docs_fetch.py book    https://docs.crowdstrike.com/r/en-US/g6auvcg3 --subdir falcon-management

## Why this reads the network rather than the DOM

The portal is Fluid Topics, and the article body lives in an **iframe**: `document.body`
carries about 20 characters of text on a fully rendered page. Every DOM-based approach
returns the navigation sidebar and nothing else -- which is exactly what the first version of
this tool did for seventeen pages before the suspiciously uniform file sizes gave it away.

What works is reading the API the page itself calls:

    /api/khub/maps/<mapId>/topics/<topicId>/content?target=DESIGNED_READER

That is the same principle the lab uses to grade console exercises: an SPA renders a view of
JSON it already fetched, and the JSON is easier to read and more faithful than the DOM built
from it. Loading one chapter triggers all of its topics, so a single navigation yields a
dozen articles.

Two other things that cost time and are worth not rediscovering:

- `settle()` is the wrong readiness signal. The page sits perfectly quiet while displaying
  "Loading application...". Wait for the content requests to stop arriving instead.
- `innerText` does not descend into shadow roots, so a readiness probe built on it reports an
  empty page even when the site has fully rendered.

Output goes to ~/falcon-docs, outside the repo and gitignored. This is CrowdStrike's
copyrighted documentation: reference material for whoever ran the fetch, never something to
commit -- least of all to a repo meant to be cloned by other people.
"""
import argparse
import asyncio
import html
import os
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agent import cdp  # noqa: E402
from observe import NetworkRecorder  # noqa: E402

OUT = pathlib.Path(os.environ.get("DOCS_OUT", pathlib.Path.home() / "falcon-docs"))
DELAY = float(os.environ.get("DOCS_DELAY", "2.5"))   # a vendor portal, not a scraping target

CONTENT_MATCH = "/content?target=DESIGNED_READER"


def to_text(raw):
    """Fluid Topics returns HTML fragments. Keep block structure, drop the markup."""
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    h = re.sub(r"(?i)</(p|li|h[1-6]|tr|div|section)>", "\n", h)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)<t[dh][^>]*>", " | ", h)
    h = re.sub(r"<[^>]+>", "", h)
    h = html.unescape(h)
    out, blank = [], 0
    for ln in (re.sub(r"[ \t]+", " ", x).strip() for x in h.splitlines()):
        if not ln:
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(ln)
    return "\n".join(out).strip()


def slug(url):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", url.split("docs.crowdstrike.com")[-1].split("?")[0])
    return s.strip("-") or "index"


async def harvest(c, net, url, min_wait=10, max_wait=60, quiet_rounds=3):
    """Navigate and collect every topic body the reader fetches.

    Waits for content requests to STOP arriving rather than for a fixed time -- a long
    chapter keeps loading topics well after the page looks finished.
    """
    await c.send("Page.navigate", {"url": url})
    seen, stable, t0 = 0, 0, time.time()
    while True:
        await asyncio.sleep(2)
        await net.fetch_bodies(c, match="/topics/", limit=200)
        now = len(net.bodies(match=CONTENT_MATCH, min_len=80))
        stable = stable + 1 if now == seen else 0
        seen = now
        elapsed = time.time() - t0
        if elapsed > max_wait:
            break
        if elapsed > min_wait and stable >= quiet_rounds:
            break
    return net.bodies(match=CONTENT_MATCH, min_len=80)


def save(url, title, bodies, subdir=""):
    dest = OUT / subdir if subdir else OUT
    dest.mkdir(parents=True, exist_ok=True)
    parts = [f"# {title}", url, ""]
    for b in bodies:
        t = to_text(b["body"])
        if len(t) >= 40:
            parts += [t, ""]
    text = "\n".join(parts).strip() + "\n"
    p = dest / (slug(url) + ".txt")
    p.write_text(text, encoding="utf-8")
    return p, len(text)


async def page_title(c):
    try:
        return await cdp.evaluate(c, "document.title") or ""
    except Exception:
        return ""


async def chapter_links(c, book_id):
    js = ("(() => { const out=[], seen=new Set();"
          " function walk(r){ let e=[]; try{e=Array.from(r.querySelectorAll('*'));}catch(x){return;}"
          "  for(const el of e){"
          "   if(el.tagName==='A' && el.href && el.href.includes('/" + book_id + "/')"
          "      && !seen.has(el.href)){ seen.add(el.href); out.push(el.href); }"
          "   if(el.shadowRoot) walk(el.shadowRoot); } }"
          " walk(document); return JSON.stringify(out); })()")
    r = await cdp.eval_json(c, js)
    return r if isinstance(r, list) else []


def _reset(net):
    net.reqs.clear()
    net.order.clear()
    net.finished.clear()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["chapter", "book"])
    ap.add_argument("url")
    ap.add_argument("--subdir", default="")
    ap.add_argument("--limit", type=int, default=30)
    a = ap.parse_args()

    tid, ws = await cdp.open_url("about:blank")
    try:
        async with cdp.Client(ws) as c:
            await asyncio.sleep(0.5)
            net = NetworkRecorder()
            net.attach(c)
            await c.send("Network.enable", {})

            if a.mode == "chapter":
                bodies = await harvest(c, net, a.url)
                p, n = save(a.url, await page_title(c), bodies, a.subdir)
                print(f"{len(bodies)} topics, {n} chars -> {p}", flush=True)
                return

            book_id = a.url.rstrip("/").split("/")[-1]
            bodies = await harvest(c, net, a.url)
            links = await chapter_links(c, book_id)
            print(f"book {book_id}: {len(links)} chapters", flush=True)
            if bodies:
                p, n = save(a.url, await page_title(c), bodies, a.subdir)
                print(f"  {len(bodies):>3} topics {n:>7} chars  (landing)", flush=True)

            # Worklist rather than a fixed list: some chapters are collapsible parents whose
            # sub-pages only appear in the DOM once the parent has been loaded. Without this,
            # "Users and Roles" (CCFA domain 1) yields 121 characters and looks done.
            done, total = {a.url}, 0
            queue = list(links)
            while queue and len(done) <= a.limit:
                link = queue.pop(0)
                if link in done:
                    continue
                done.add(link)
                _reset(net)
                try:
                    bodies = await harvest(c, net, link)
                except Exception as e:  # noqa: BLE001
                    print(f"  FAIL {link[-10:]} {str(e)[:50]}", flush=True)
                    continue
                if not bodies:
                    print(f"  --- no topics  {link[-10:]}", flush=True)
                    continue
                title = await page_title(c)
                p, n = save(link, title, bodies, a.subdir)
                total += n
                # Loading this chapter may have revealed sub-pages; queue any new ones.
                for nl in await chapter_links(c, book_id):
                    if nl not in done and nl not in queue:
                        queue.append(nl)
                print(f"  {len(bodies):>3} topics {n:>7} chars  "
                      f"{title.split('•')[0].strip()[:46]}", flush=True)
                time.sleep(DELAY)
            print(f"saved {total} chars to {OUT / a.subdir if a.subdir else OUT}", flush=True)
    finally:
        await cdp.close_target(tid)


if __name__ == "__main__":
    asyncio.run(main())
