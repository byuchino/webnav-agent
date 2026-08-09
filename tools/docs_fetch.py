#!/usr/bin/env python3
"""Fetch CrowdStrike documentation pages through an authenticated browser session.

The docs portal is behind SSO, so this drives a Chrome that a human has already signed into
(see README, "Driving a remote browser"). It reads; it never clicks anything that changes
state.

Two things shape the implementation:

- **The site is shadow-DOM throughout.** Ordinary `document.querySelectorAll('a')` misses
  almost every link, and the v4 snapshot renders the nav with empty labels because the text
  sits in nested shadow roots. Both the link walker and the text extractor pierce shadow
  roots explicitly.
- **Output goes outside the repo.** This is CrowdStrike's copyrighted documentation. It is
  reference material for whoever ran the fetch, not something to commit -- least of all in a
  repo meant to be cloned by other people.

  ./tools/docs_fetch.py toc  https://docs.crowdstrike.com/r/en-US/g6auvcg3
  ./tools/docs_fetch.py page https://docs.crowdstrike.com/r/en-US/g6auvcg3/abc123
  ./tools/docs_fetch.py book https://docs.crowdstrike.com/r/en-US/g6auvcg3 --limit 40
"""
import argparse
import asyncio
import os
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agent import cdp, snapshot  # noqa: E402

OUT = pathlib.Path(os.environ.get("DOCS_OUT", pathlib.Path.home() / "falcon-docs"))

# Be a considerate client: this is a vendor portal, not a scraping target.
DELAY = float(os.environ.get("DOCS_DELAY", "2.0"))

_LINKS_JS = r"""
(() => {
  const out = [], seen = new Set();
  function label(el){
    let t = (el.innerText || el.textContent || '').trim();
    if (!t && el.shadowRoot) t = (el.shadowRoot.textContent || '').trim();
    if (!t) t = (el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
    if (!t) { let p = el.parentElement, n = 0;
              while (p && n < 3 && !t) { t = (p.innerText || '').trim(); p = p.parentElement; n++; } }
    return t.replace(/\s+/g, ' ').slice(0, 120);
  }
  function walk(root){
    let els = [];
    try { els = Array.from(root.querySelectorAll('*')); } catch(e){ return; }
    for (const el of els) {
      if (el.tagName === 'A' && el.href && !seen.has(el.href)) {
        seen.add(el.href);
        out.push({t: label(el), h: el.href});
      }
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  }
  walk(document);
  return JSON.stringify(out);
})()
"""

# Page-as-document extraction (guide §9): strip chrome, keep the article. Shadow-aware,
# because on this site the article body itself is usually inside a shadow root.
_TEXT_JS = r"""
(() => {
  function deepText(root, depth){
    if (depth > 12) return '';
    let parts = [];
    for (const el of Array.from(root.children || [])) {
      const tag = el.tagName;
      if (['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','NAV','HEADER','FOOTER','ASIDE'].includes(tag))
        continue;
      if (el.shadowRoot) {
        parts.push(deepText(el.shadowRoot, depth + 1));
      } else if (el.children && el.children.length) {
        parts.push(deepText(el, depth + 1));
      } else {
        const t = (el.innerText || el.textContent || '').trim();
        if (t) parts.push(t);
      }
    }
    let s = parts.join('\n');
    if (!s.trim()) s = (root.innerText || root.textContent || '').trim();
    return s;
  }
  const main = document.querySelector('main, [role=main], article') || document.body;
  const txt = deepText(main, 0);
  return JSON.stringify({
    title: document.title,
    url: location.href,
    text: txt.replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim().slice(0, 200000)
  });
})()
"""


def slug(url):
    return re.sub(r"[^a-zA-Z0-9]+", "-", url.split("docs.crowdstrike.com")[-1]).strip("-") or "index"


# innerText does NOT descend into shadow roots, and on this site essentially all content
# lives in them -- so body.innerText shows "Loading application..." on a fully rendered page.
# The v4 snapshot builder already pierces shadow DOM correctly, so use it as the probe.
async def _rendered(c):
    env = await snapshot.build(c, mark=False)
    tree = (env.get("tree") or "")
    return env, len(tree), ("Loading application" in tree and len(tree) < 1200)


async def _load(c, url, settle_ms=8000, pause=2.0, limit=40):
    """Navigate and wait for CONTENT, not for quiet.

    settle() waits for the page to stop changing, and this site is perfectly quiet while
    still showing "Loading application...". Quiescence is the wrong signal for an SPA that
    renders after its network goes idle -- poll for real text instead.
    """
    await c.send("Page.navigate", {"url": url})
    await asyncio.sleep(pause)
    await snapshot.settle(c, max_ms=settle_ms)
    deadline = time.time() + limit
    while time.time() < deadline:
        env, n, loading = await _rendered(c)
        if n > 800 and not loading:
            return env
        await asyncio.sleep(1.5)
    return None


async def fetch_toc(c, url):
    await _load(c, url)
    links = await cdp.eval_json(c, _LINKS_JS)
    if not isinstance(links, list):
        return []
    base = url.rstrip("/")
    # Pages within this book share its id prefix.
    book = base.split("/")[-1]
    out, seen = [], set()
    for l in links:
        h = l.get("h", "")
        if f"/{book}/" in h and h not in seen:
            seen.add(h)
            out.append(l)
    return out


async def fetch_page(c, url):
    env = await _load(c, url)
    if not env:
        return None
    return {"title": env.get("title", ""), "url": env.get("url", url),
            "text": (env.get("tree") or "").strip()}


_DROP = re.compile(r"^\s*(\|shadow:|\[\d+\]\s)")


def clean(tree):
    """The v4 tree is built for an agent choosing what to click; as reference prose the
    control markers and shadow-boundary lines are noise. Keep the text, drop the scaffolding."""
    out, blank = [], 0
    for line in tree.splitlines():
        if _DROP.match(line):
            continue
        t = line.strip()
        if not t:
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(t)
    return "\n".join(out).strip()


def save(d, subdir=""):
    dest = OUT / subdir if subdir else OUT
    dest.mkdir(parents=True, exist_ok=True)
    p = dest / (slug(d["url"]) + ".txt")
    p.write_text(f"# {d['title']}\n{d['url']}\n\n{clean(d['text'])}\n", encoding="utf-8")
    return p


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["toc", "page", "book"])
    ap.add_argument("url")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--subdir", default="")
    a = ap.parse_args()

    tid, ws = await cdp.open_url("about:blank")
    try:
        async with cdp.Client(ws) as c:
            await asyncio.sleep(0.5)

            if a.mode == "toc":
                toc = await fetch_toc(c, a.url)
                print(f"{len(toc)} pages in this book")
                for l in toc:
                    print(f"  {(l['t'] or '-')[:62]:64} {l['h'].split('docs.crowdstrike.com')[-1]}")

            elif a.mode == "page":
                d = await fetch_page(c, a.url)
                if not d:
                    sys.exit("no text extracted")
                p = save(d, a.subdir)
                print(f"{len(d['text']):>7} chars  {d['title'][:60]}\n  -> {p}")

            else:  # book
                toc = await fetch_toc(c, a.url)
                print(f"{len(toc)} pages; fetching up to {a.limit} (delay {DELAY}s)")
                got = 0
                for l in toc[:a.limit]:
                    try:
                        d = await fetch_page(c, l["h"])
                    except Exception as e:  # noqa: BLE001
                        print(f"  FAIL {l['h'][-24:]}  {str(e)[:60]}")
                        continue
                    if not d or len(d["text"]) < 200:
                        print(f"  thin {(l['t'] or '-')[:50]}")
                        continue
                    p = save(d, a.subdir)
                    got += 1
                    print(f"  {len(d['text']):>7} chars  {(d['title'] or '')[:56]}")
                    time.sleep(DELAY)
                print(f"saved {got} pages to {OUT / a.subdir if a.subdir else OUT}")
    finally:
        await cdp.close_target(tid)


if __name__ == "__main__":
    asyncio.run(main())
