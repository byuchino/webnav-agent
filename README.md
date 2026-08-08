# CDP web-navigation agent (Gemma 4 · local)

A web-navigation agent built from `WEB_NAVIGATION_TECHNIQUES.md`, driving Chrome over the
Chrome DevTools Protocol with Gemma 4 E4B served locally.

This is **Phase 1** of the guide's §21 build order — "the load-bearing core, 80% of the
value" — plus the three highest-value macros from Phase 2.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install openai cdp-use websockets

# Chrome, with a THROWAWAY profile — never debug your logged-in daily browser (§16.5)
google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/cdp-profile \
              --headless=new --no-first-run about:blank &
```

The model server is LM Studio on `192.168.254.26:1234`, model `google/gemma-4-e4b`
(configured in `agent/llm.py`).

## Use

```bash
./run.py --snapshot fixtures/cart.html                 # observation only, no model
./run.py "How many items are in the cart?" fixtures/cart.html
./run.py "Remove the Gadget item" fixtures/cart.html --allow example.com

.venv/bin/python tests/test_guards.py                  # policy tests, no model/browser
.venv/bin/python tests/smoke.py                        # end-to-end, needs both
```

## Layout

| File | Guide § | What |
|---|---|---|
| `agent/cdp.py` | §3 | Transport, exception surfacing, trusted input, screenshots |
| `agent/snapshot.py` | §4, §5, §6 | v4 observation, settle, act-by-index with staleness |
| `agent/skills.py` | §7, §8, §17 | Intent macros, guarded `eval_js`, navigation allowlist |
| `agent/llm.py` | §14, §16.1 | Model I/O contract, system prompt, op menu |
| `agent/loose_json.py` | §14.3 | Forgiving parse and repair |
| `agent/agent.py` | §15.1 | Observe/decide/act/report loop, dispatcher |

## Driving a remote browser (and authenticated sessions)

The agent can drive Chrome on another machine — useful when that machine holds the sessions
you need, or is where the model already lives.

**The tunnel is required, not a convenience.** Chrome's DevTools endpoint rejects any request
whose `Host` header isn't loopback (*"Host header is specified and is not an IP address or
localhost"*), so opening 9222 on the remote firewall does not work. An SSH local forward makes
it look local — and keeps a full-browser-control port off the LAN, which matters far more once
the profile is authenticated:

```bash
ssh -N -L 9333:127.0.0.1:9222 <host>       # keep this running
CDP_PORT=9333 ./run.py --snapshot https://example.com/
```

`CDP_PORT` (default 9222) is the only knob; local and remote browsers can run side by side.

**Launching Chrome remotely.** On Windows, `Start-Process` over SSH is not enough — OpenSSH
kills child processes when the session ends, and the browser dies the moment the command
returns. Spawn it detached via WMI instead:

```powershell
$cmd = '"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir=C:\cdp-profile --no-first-run --no-default-browser-check --headless=new about:blank'
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=$cmd}
```

A separate `--user-data-dir` means this coexists with the daily browser; neither disturbs the
other.

### Signing in

Headless Chrome launched over SSH has no visible window, so log in from the machine's own
desktop, once:

1. Close any Chrome using `C:\cdp-profile` (Chrome locks a profile directory to one instance).
2. On the desktop, launch Chrome **headed** with the same `--user-data-dir=C:\cdp-profile` and
   sign in to only the sites the task needs. Close it.
3. Relaunch headless with the WMI command above. Cookies persist in the profile.

### Posture for authenticated runs

§17 mitigation 1 — *"a dedicated profile with no valuable sessions"* — is the cheapest
protection there is, and signing in deliberately gives it up. What remains has to carry more:

```bash
CDP_PORT=9333 ./run.py --confirm writes --no-eval-js --allow example.com "..."
```

- **A dedicated profile signed into only what the task needs.** Never the daily profile: this
  agent has been observed obeying an injected instruction on step 0, every run.
- **`--confirm writes`**, not `destructive`. The wording match is a tripwire and misses
  "Continue" as the last step of a purchase.
- **`--no-eval-js`.** Model-authored JavaScript is the widest remaining hole.
- **Know what the allowlist cannot do.** It stops the agent *reaching* a hostile origin. It
  cannot see an injection hosted *on the site you are logged into* — a comment, an email body,
  a search-result snippet — which is precisely where the session lives. Only the consent gate
  stands between that and an irreversible action, which is why `writes` is the honest setting.

## Op menu

13 of the guide's 23 ops (Appendix A). `assert_menu_consistent()` runs at import and fails
startup if the documented menu and the dispatcher ever drift apart — the guide's reference
advertised `type_text` and `drag_to` with no handler for either.

```
click  check  setval  submit  scroll  navigate  wait  report
eval_js  extract_jsonld  click_by_text  click_in_section  fill_labeled_input  wait_for_text
```

Give the **full** menu, always. §20 measured op-gating (showing only the 4–8 relevant ops
per task) at **26 → 20/38 and rejected it** — adding ops was neutral-to-positive, removing
them was clearly negative.

Not yet built: `type_text`, `drag_to`, `select_option_by_text`, `scroll_until_found`,
`fill_form`, `read_widget_state`, `sum_across_pages`, `state_*`, `search_page`.

## Deviations from the guide

The guide targets llama.cpp's `llama-server`. On LM Studio three things differ, all
verified against the live server:

1. **`response_format`** — §14.1's `{"type":"json_object"}` is rejected: *"'response_format.type'
   must be 'json_schema' or 'text'"*. We send a real `json_schema` instead, which is strictly
   better: it constrains the op **enum** and field types, not just syntax.
2. **Thinking mode** — §14.2's empty-thinking fallback is kept, but `gemma-4-e4b` here reports
   `reasoning_tokens: 0`, so thinking is not eating the output budget. The fallback is cheap
   insurance against a silent failure, not a live fix.
3. **Loose-JSON repair** — §14.3 calls it redundant under constrained decoding. It is **not**
   redundant here: unconstrained calls come back wrapped in ```json fences.

## Guide bugs deliberately not reproduced

Each is marked ⚠️ in the source document:

- **§6** Action results are surfaced to the model, never discarded. *"An action layer that
  cannot report failure is not an action layer; it is a random number generator."*
- **§4.6** `rowCtx()` is memoized per container. The reference clones a DOM subtree per
  interactive element, which at 600 controls dominates the 600 ms budget.
- **§16.3** Stale `data-snap-*` attributes are cleaned using the **previous** tokens. The
  reference cleans with the *new* token, so it removes nothing and they accumulate forever.
- **§4.5 O6** Checkbox `value="on"` is suppressed — it is a default, and defaults are noise.

## Defects found while building this

Things the guide did not anticipate, found by running it:

- **Appendix A's `assert_menu_consistent` is asymmetric.** It exempts `report` from `extra`
  but not from `missing`, so it trips on any menu that documents `report` — which is every
  menu. `report` is terminal and never reaches the dispatcher. Fixed via `TERMINAL_OPS`.
- **§14.5 schema-key signaling backfires as a flat schema field.** `exact_value_only_no_prose`
  is the most descriptively-named key on the menu, so the model reaches for it as "the place
  a value goes" on *fill* ops too, not just `report` — filling the field with `""`. The
  dispatcher now accepts `value` / `text` / `exact_value_only_no_prose` as one family.
- **No stuck-loop watchdog.** §16.2's budget table caps total steps but nothing detects the
  same failing action repeating. Observed burning all 8 steps on one bad fill. Added
  `REPEAT_WARN` / `REPEAT_STOP`, which announce themselves to the model per §16.2's meta-rule.
- **§17's own attack works.** The 1px white-on-white injection in `fixtures/injection.html`
  is obeyed by the model on step 0, every time — it emits the injected `navigate`. Only the
  deterministic allowlist stops it. Confirms the guide: *"a 4B model's weak instruction-following
  is not protection."*

## Findings from real pages (finance.yahoo.com)

The guide's §4.6 warns its budgets were tuned on hand-written fixtures and are untested at
scale. They are worse than untested — two design decisions actively break on real pages:

- **`display:contents` pruned the entire subtree.** Those wrappers have zero-size rects, so
  the guide's `vis()` returns false and the walker skips the subtree. But `display:contents`
  exists precisely to remove the box while children lay out normally. On a fully hydrated
  25 KB Yahoo quote page this yielded **125 nodes visited, 9 lines, no price**. Fixed with a
  three-way `visKind()`: `hidden` / `visible` / `passthrough`, where passthrough descends
  without emitting. Same page after: **3,404 nodes, 229 controls, 520 lines, 43 ms.**
- **Greedy salient containers.** A `DIV`/`SPAN` is "salient" merely by carrying `tabindex` or
  `onclick`, gets marked as one control, and is never recursed into. An entire market-summary
  region collapsed to a single `[7] div "US Markets S&P 500 ..."` line. Generic containers are
  now only treated as controls when small and holding nothing else interactive.
- **In-page `el.click()` silently does nothing on real sites.** §3 warns about `isTrusted:false`
  and even gives the hybrid fix — which the reference only uses on its vision path, and which
  I had written in `cdp.py` and failed to wire up. On yahoo.com the model clicked the correct
  search-submit button, got `{"ok":"clicked"}`, and the page did not react. `snapshot.click()`
  and both click macros now resolve the element in-page and dispatch a **trusted** mouse event
  at its centre, falling back to the in-page click only for unusable boxes.
- **`submit` was missing from the op menu** even though Appendix A documents it. After filling
  a search box a small model has to find and click the right button — §15.3 exists entirely to
  avoid that. Added as real CDP Enter-key input on the focused field.

### The failure the verification layer would have caught

With all of the above fixed, the agent navigated yahoo.com -> Finance -> search "CRWD"
unaided, and reported **3,626.00**. That number is really on the page:

```
[110] link "CRWD.MX  CrowdStrike Holdings, Inc.  3,626.00  +82.70 (+2.33%)"
```

It is CrowdStrike on the **Mexican exchange, in pesos**. The NASDAQ USD price was 214.42.
Not a hallucination — correct extraction from the wrong instrument, and nothing in the
system can tell the difference. This is §16.4 exactly: an action's success is inferred from
the model having said something. Build the verification layer before trusting any answer.

## Security posture (§17)

Built in from day one, not bolted on:

- **Throwaway Chrome profile** with no valuable sessions (mitigation 1).
- **Navigation allowlist that fails closed** (mitigation 2) — an absent or empty allowlist
  denies *every* absolute URL. `run()` seeds it with the start URL's own origin. An earlier
  default-open version was defeated immediately; default-open is not a mitigation.
- **`eval_js` deny-list** (mitigation 3) — `fetch`, `XMLHttpRequest`, `WebSocket`,
  `EventSource`, `sendBeacon`, `import()`, `document.cookie`, `localStorage`/`sessionStorage`/
  `indexedDB`. Every expression is logged before it runs.
- **Explicit untrusted-data boundary** (mitigation 4) — the page tree is wrapped in
  `<<<BEGIN_UNTRUSTED_PAGE_DATA>>>` and the system prompt states nothing inside is an instruction.
- **In-page redaction** (§4.5 O9) — password and secret-ish field values are replaced with
  `[redacted]` *before* they cross the wire, so they never reach the prompt, the log, or the
  model server.
- **Human-in-the-loop for irreversible actions** (mitigation 6) — `agent/consent.py`. Four
  policies, selected with `--confirm`:

  | Policy | Behaviour |
  |---|---|
  | `auto` | Never asks. For anonymous fixtures and read-only scraping. |
  | `destructive` | **Default.** Asks when the target's wording looks irreversible. |
  | `writes` | Asks before every mutation, whatever it is called. |
  | `readonly` | Refuses every mutation. Links may still be followed. |

  It **fails closed**: with no interactive terminal there is no human to ask, so the answer
  is no. Refusals reach the model as a `TOOL RESULT`, exactly like a blocked navigation, so
  it can route around instead of retrying blindly.

  Index ops are checked before dispatch from the snapshot's own control metadata; the macros
  carry the gate inward as a callback and check **after resolving, before clicking** — which
  is only possible because they already resolve with `doClick=false` first. `click_in_section`
  also passes its section as row context, so "Remove" is judged differently in
  "Visa ending 4242" than in a list of filters.

  The name match is a tripwire, not a proof — "Continue" can be the last step of a purchase.
  Anything that genuinely must not happen unattended belongs under `writes` or `readonly`.

## Status

**Fixtures: 6/6 smoke cases pass** — read, `click_in_section` against three identical Remove
buttons, `eval_js` aggregate, fill, JSON-LD extraction, and the injection case. Plus **59
guardrail tests** and a consent end-to-end suite that proves no side effect fires — with the
control condition that under `auto` the same click *does* fire, so a broken fixture cannot
masquerade as a working gate. Snapshot of the cart fixture: **4 ms**, 59 nodes.

**Real site, unaided task: FAILED.** Given "what is CrowdStrike's share price?" starting at
`yahoo.com`, the agent searched, reached Finance, and reported the price of the *wrong
listing* (see above). An earlier blind run failed outright with `N/A` because clicks were
untrusted no-ops. The mechanical layers work on a real page; the final grounding decision is
unverified and was wrong.

Track these two separately and do not average them. Per §19.5 macro **correctness** and
model **invocation rate** fail independently and need different fixes — and the same split
applies to "the harness acted" versus "the answer was right".

All of these are **single runs at temperature 0**, which per §19.4 is not a score. The guide
saw tasks flip between runs at temperature 0, so any +1/+2 delta is uninterpretable. Real
numbers need N=5 and mean ± σ.

## Next (guide §21)

1. **The verification layer (§16.4)** — Phase 3 step 10, but promoted to first on the
   evidence above. The CRWD.MX failure is precisely what it exists to catch, and it is the
   prerequisite for episodic memory. For this case it is cheap: assert the landed URL
   contains the requested symbol and the quote header matches the ticker asked for. Wire
   failures back to the model as a `TOOL RESULT`.
2. **Relevance-ordered truncation (§4.6 weakness 3).** The Yahoo quote page renders to
   **7,434 chars against `MAX_RENDER_CHARS = 7000`**, so the tail is already being clipped.
   It did not matter there — the price sits near the top — but truncation is document-order,
   and "Submit" usually lives at the bottom. Two-pass build: walk cheaply to count, then
   prioritise interactive and viewport-proximate content when over budget.
3. **Measure the snapshot on 20 real pages (§4.6).** Two real pages have already produced two
   structural bugs (`display:contents`, greedy salient containers). Assume more. Record
   `visited`, `controls`, elapsed ms and `truncated` for each.
4. **Remaining macros (§7)** as the task distribution demands: `type_text`,
   `select_option_by_text`, `scroll_until_found`, `sum_across_pages`.
5. **Episodic memory (§11) only after verification exists.** Gating memory writes on "the
   model reported something" makes the flywheel spin whichever way the first run went — and
   the first run here would have written CRWD.MX in as a same-site success recipe.
