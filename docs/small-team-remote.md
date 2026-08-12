# Small-team remote access: the pragmatic hybrid

A companion to `distribution-architecture.md` and `multi-user-analysis.md`. This is the
right-sized middle ground: **distribute the AI assistant** (each user runs it locally) while
keeping **one shared lab instance** that a couple of **remote** colleagues use **strictly one at
a time**. Design note only; nothing built.

## Why this is far more tractable than full multi-user

"One user at a time" eliminates the two hard problems from `multi-user-analysis.md`: no per-user
VMs and no per-user CID namespacing, because the shared state is only ever touched by one person
at a time — exactly like today, with a rotating occupant. The existing lab, scenarios, and
grading work **unchanged**. What remains is *access and coordination*, which is plumbing, not a
core refactor.

## What already works in your favour

- **The Falcon console is already remote** — it is SaaS (`falcon.us-2.crowdstrike.com`). A remote
  colleague logs in from their own browser; no tunnel or VPN needed for the console itself.
- **RTR is cloud-mediated** — reaching the guests to run commands happens through the console, so
  remote users get full RTR with no local-network access.
- **The panel hides the private network** — `lab.py`/the panel run next to Proxmox and do the SSH
  themselves; a remote user driving the panel never touches `10.77.0.0/24` directly. The panel is
  the abstraction boundary.
- **The assistant is simpler remote** — today it reaches a signed-in Chrome on a separate box over
  an SSH tunnel; a colleague running the browser *and* the assistant on their own laptop lets
  `observe.py` attach to the **local** browser's debug port, no tunnel at all.

## What has to be built or decided

1. **Remote access to the panel** (the one thing that must become reachable):
   - **Tailscale/WireGuard (recommended)** — the colleagues join a private tailnet including the
     panel (and, with subnet routing, the guests). Easiest for 2-3 remote people, encrypted, no
     public exposure.
   - **Cloudflare Tunnel / reverse proxy with auth** — expose the panel to the internet behind
     strong auth. Simpler onboarding, larger attack surface.
   - Direct port-forward — do not (see security).

2. **Panel authentication — now mandatory.** The panel is auth-free today because it is
   single-user on a trusted LAN. Once remote-reachable that is dangerous: **it can revert VMs and
   execute code on guests, and it sits next to a Falcon API key.** It needs a login before any
   exposure, ideally plus a VPN. This is the single most important change.

3. **A lightweight turn-taking lock.** "Strictly one at a time" needs a claim/release in the
   panel — "in use by Alice", others wait, with a timeout so a walked-away session frees up. For
   2-3 people this is modest software (or even an informal "I've got it" channel). It is *needed*,
   not just polite: the shared CID objects (`Falcon Lab` group, `Falcon Lab IOA` rule group,
   quarantine state) would confuse a second concurrent user, and a mid-exercise revert would wipe
   their work.

4. **CID logins for the colleagues — the sharpest real decision.** They need accounts in the
   Falcon CID to do the exercises. The lab currently uses a **production CID** with a dedicated
   host group. Giving remote colleagues logins — even scoped — is a governance call: their roles
   must be tight (enough for the labs, nothing near production data), and ideally this is a
   **dedicated lab/trial CID** rather than production. This is a trust-and-blast-radius question,
   not a technical one, and it is the thing to settle first.

5. **Docs for each colleague's local assistant.** Each runs the assistant locally, which needs
   the docs. For a couple of trusted colleagues, sharing the harvested copy is a smaller concern
   than public redistribution — but it is still CrowdStrike's content, so the clean answer stays
   "they harvest their own" (they can, since they now have CID logins). See `D1` in
   `distribution-architecture.md`.

## The edge case worth naming

A few exercises still reach the guest by **direct SSH** (sensor-install especially — a bare host
has no sensor, so no RTR). A remote user cannot do those unless the VPN also **subnet-routes to
`10.77.0.0/24`** (Tailscale can), or those exercises stay owner-only. Everything RTR- and
console-based is fine remotely; the bare-metal ones are the exception.

## Access choice: Tailscale vs Cloudflare Tunnel

The unlock for both is the **in-panel SSH terminal** (below): it makes the panel the *single*
exposed surface (HTTP/WS only), which removes the need for network-level guest access.

**Tailscale (you already run a personal tailnet for NAS sync):**
- One tailnet per account — there is no "project tailnet". Reusing the personal tailnet means it
  also holds your NAS and laptop, so **ACLs become safety-critical** (default-deny, allow only
  colleague → panel).
- Add colleagues by **node-sharing** the single panel machine to their *own* free Tailscale
  account (scoped to that node, does not consume your user seats) — this is the $0 path — rather
  than inviting them as users into your tailnet.
- **Caveat 1:** Tailscale authenticates the *network*, not the *app*. Anyone who can reach the
  panel over the tailnet hits it with **no login**, so you still have to build panel auth (or
  rely on ACLs + trusting the device). Device-level, not person-level; no session/MFA/revocation.
- **Caveat 2:** subnet-routing to the guests for a *shared external* user is awkward — solved by
  the in-panel terminal, which is why that feature matters here.

**Cloudflare Tunnel + Access:**
- No open ports (`cloudflared` dials out). **Cloudflare Access** puts *person-level* auth at the
  edge — email allow-list, MFA, session expiry, one-click revocation — which **solves the
  mandatory panel-auth requirement for free**, the thing Tailscale does not.
- Total isolation from your personal devices (not a network overlay), so zero blast-radius to the
  NAS/laptop.
- Free tier covers well past a couple of users; the one prerequisite/cost is a **domain on
  Cloudflare** (~$8–10/yr if you do not already have one). Tailscale is truly $0; Cloudflare is
  $0 *if you already own a domain*.

**Decision:** build the in-panel SSH terminal **regardless**. Then — have a domain → Cloudflare
Tunnel + Access + terminal (best auth posture, isolates colleagues from personal devices);
strictly $0 / no domain → Tailscale node-share + tight ACLs + terminal, and build app-level panel
auth yourself. Lean: **Cloudflare** for this use — edge person-auth is exactly what the threat
model needs.

## Read

Achievable **without touching the lab's core** — no scenario templating, no namespacing, no extra
VMs, no sensor-license multiplication. The work is:

- **plumbing:** a VPN (Tailscale) + panel auth + a claim-lock — days, not weeks;
- **one governance decision:** how colleagues get into the CID (dedicated lab CID ≫ scoped roles
  on production);
- **the assistant:** actually easier here (local browser, local docs), and it is the same
  distributable component from `distribution-architecture.md`, so this hybrid and the split
  reinforce each other.

The dividing line is clean: **the assistant distributes and runs local; the lab stays a single
shared instance, hosted by the owner, reached over a VPN, used one-at-a-time.** Mostly off-the-
shelf pieces (Tailscale, auth middleware, a lock) rather than novel engineering. The part that
would actually bite is not code — it is the CID access decision. Settle that first.
