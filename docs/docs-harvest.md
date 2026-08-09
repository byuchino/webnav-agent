# Harvesting the CrowdStrike documentation

Reference material for writing lab scenarios and for answering questions during an exercise.
The CCFA Certification Guide names five documentation titles as recommended reading; this is
how they get onto disk.

```bash
# 1. On the Windows box, HEADED, at the desktop (not over SSH -- you must see the SSO prompt)
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir=C:\cdp-profile --no-first-run --no-default-browser-check
#    then sign in to docs.crowdstrike.com and leave it open

# 2. From the workstation
ssh -N -L 9333:127.0.0.1:9222 gaming-pc &
CDP_PORT=9333 ./observe.py --list          # confirm the session is live
./tools/harvest_all.sh                     # ~1 hour, unattended
```

Output lands in `~/falcon-docs`, **outside the repo and gitignored**. This is CrowdStrike's
copyrighted documentation: reference for whoever ran the fetch, never something to commit —
least of all to a repo meant to be cloned by other people.

## Why it reads the network, not the page

The portal runs Fluid Topics and renders the article body **inside an iframe**, leaving
`document.body` with about 20 characters of text on a fully loaded page. Every DOM-based
approach returns the navigation sidebar and nothing else — which the first version of this
tool did for seventeen pages before the suspiciously uniform file sizes gave it away.

What works is reading the API the page itself calls:

```
/api/khub/maps/<mapId>/topics/<topicId>/content?target=DESIGNED_READER
```

This is the same mechanism the lab uses to grade console exercises. An SPA renders a view of
JSON it already fetched; reading that JSON is both easier and more faithful than parsing the
DOM built from it. Loading one chapter triggers all of its topics, so a single navigation
yields a dozen articles.

## Four things that cost time, worth not rediscovering

| Symptom | Cause |
|---|---|
| `curl` returns 200, looks public | That is the **login page** rendering. The agent's snapshot showed the real title: "Sign In". |
| Link extraction finds nothing | The nav is shadow DOM throughout; `document.querySelectorAll('a')` misses nearly all of it. |
| Page "never loads" | `settle()` is the wrong signal — the page sits perfectly quiet showing "Loading application...". Wait for content requests to stop arriving. |
| Readiness probe sees an empty page | `innerText` does not descend into shadow roots. |

A fifth, subtler one: some chapters are collapsible parents whose sub-pages only enter the DOM
once the parent has loaded. The crawl therefore uses a **worklist**, re-scanning for links
after each page. Without it, "Users and Roles" yields 121 characters and looks complete.

## Guards for unattended runs

- **Session expiry.** A batch outlives an SSO session. `docs_fetch` aborts a book as soon as a
  page title comes back as "Sign In", rather than writing hundreds of files full of the login
  page. Look for `SESSION EXPIRED` in `~/falcon-docs/harvest.log`.
- **Per-book timeout**, so one hanging chapter cannot consume the whole run.
- **Rate.** `DOCS_DELAY` (default 2.5s) between pages. This is a vendor portal, not a scraping
  target; fetch the sections the guide names rather than mirroring the site.

## What the full run produced (2026-08-09)

All five books, no session expiry, ~80 minutes: **378 files, 3.1 MB.**

| Directory | Files | Size |
|---|---|---|
| `falcon-management` | 121 | 1.0 MB |
| `endpoint-security` | 121 | 1.0 MB |
| `crowdstrike-apis` | 61 | 460 KB |
| `crowdstrike-store` | 41 | 460 KB |
| `audit-logs` | 34 | 140 KB |

Every exam objective has material behind it. The thinnest is **2.4 (RFM / troubleshooting)**
at 11 files and **2 (Sensor Deployment)** at 16 — worth knowing when writing those scenarios,
since sensor deployment is the one domain where the lab exercises carry more weight than the
docs do.

## Resuming

Progress is per-file, so an interrupted run loses nothing already written. Re-run
`./tools/harvest_all.sh` — completed books are simply rewritten. If only one book is missing:

```bash
CDP_PORT=9333 ./.venv/bin/python tools/docs_fetch.py book \
  https://docs.crowdstrike.com/r/en-US/<bookId> --subdir <name> --limit 120
```

| Book | Path | CCFA relevance |
|---|---|---|
| Falcon Management | `/r/en-US/g6auvcg3` | domains 1, 3, 4, 5, 7 |
| Endpoint Security | `/r/en-US/a5kj6wfu` | domains 5, 6 |
| CrowdStrike APIs | `/r/en-US/kgsgkjd3` | objective 1.3 |
| Audit Logs | `/r/en-US/dpxel4ag` | objective 7.2 |
| CrowdStrike Store | `/r/en-US/wlmfpr5u` | the guide's "Marketplace" entry |
