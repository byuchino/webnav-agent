# Multi-user support: analysis and decision

A companion to `distribution-architecture.md`. What it would take to run the lab for multiple
simultaneous users, and why we are **not** doing it now.

## Decision (2026-08-12)

**The lab stays single-user.** The hard parts of multi-user are not code — they are
resource multiplication (Proxmox capacity, Falcon sensor licenses, a crowded CID) — and the
software changes that *are* needed (identity, namespacing) would make the single-user
experience more complicated for no benefit to the primary use case. Revisit only if a concrete
multi-user need appears (e.g. an instructor-led class); the analysis below is the starting point
if so.

## Why the current system is single-tenant

Three shared resources, none isolated, hardest to easiest:

### 1. The guests — reverting is a global, destructive act (hardest)
There is one Windows guest and one Linux guest. Exercises work by **reverting the whole VM to a
snapshot** and mutating it. Proxmox snapshots are per-VM, not per-user, so the instant a second
user presses "set up" on any guest exercise, the revert **destroys the first user's in-progress
work** on that host. Any exercise that touches a guest is single-occupancy by nature.

### 2. The Falcon CID — console objects are per-CID and shared (subtle)
Even with the guests solved, grading and exercises operate on one CID's shared namespace: the
`Falcon Lab` group, the policies, the `Falcon Lab IOA` rule group, the `FalconLabMLX` exclusion,
quarantined files. Two users "create the Falcon Lab IOA group" or "assign a policy to Falcon
Lab" collide on the same object, and grading cannot tell them apart —
`quarantined_file("FALCON-LAB-WIN")` or `detection_contains("FALCONLAB-IOA-TEST")` has no idea
*whose* result it found. The grading layer assumes fixed, singular names.

### 3. The panel & identity — no auth, global state (tractable)
The panel is deliberately auth-free with a global in-memory job registry, no users, no sessions.
Fine single-user on a trusted network; the first thing to change for multi-user, and the easiest
because it is only software.

## The models, and what each costs

- **Model 1 — full per-user isolation.** Each user gets their own guests, host group, naming
  namespace, panel state. The wall is guest cost: N users = 2N VMs on one Proxmox host; capacity
  (CPU/RAM/IO/snapshot storage) caps N fast and concurrent reverts+boots hammer a single host —
  realistically needs a Proxmox *cluster* past a handful of users. Provisioning-as-code (see the
  distribution note) stops being optional. And every guest needs a Falcon **sensor license** —
  2N of them.
- **Model 2 — one CID, per-user namespacing** (nobody gets N CIDs cheaply). Suffix everything
  per user (`Falcon Lab — alice`, per-user policies, IOA groups, hostnames, detection tokens).
  The CID fills with per-user clutter that needs lifecycle management (create on enroll,
  garbage-collect on exit) or it rots; policy precedence gets hairy with N parallel sets;
  grading must be **parameterized by namespace everywhere**, which turns the scenarios (hardcoded
  names/tokens today) into per-user **templates** resolved at runtime; role exercises need a
  throwaway **test user per learner** minted in the CID.
- **Model 3 — time-sharing / booking.** One set of resources; users queue or reserve slots.
  Truly simultaneous only for console-namespaced exercises; guest exercises stay one-at-a-time.
  Cheapest infra; fits an instructor-led class where students rotate through the endpoint labs.

**The natural hybrid:** console-only scenarios (roles, policies, IOAs, exclusions) can go truly
multi-user with per-user namespacing at modest cost; endpoint scenarios (anything that reverts a
guest) need Model-1 isolation or Model-3 queuing. The catalog would split into "shareable" and
"exclusive" exercises.

## Cross-cutting refactors needed regardless of model
- **Scenarios become templates** — every hardcoded `FALCON-LAB-WIN`, `Falcon Lab`,
  `FalconLabMLX`, `FALCONLAB-IOA-TEST` becomes a per-user variable so objects and detections are
  attributable.
- **Grading gains a namespace parameter** threaded through every `kind: api` / `endpoint` check.
- **The panel gets identity** — auth, per-user session, per-user job registry, per-user host/
  namespace mapping. The "no auth by design" stance ends the moment it is network-exposed to
  several people.
- **Concurrency control on Proxmox** — a lock/queue so two users cannot operate the same VM, and
  capacity limits so N concurrent reverts do not melt the host.
- **The AI assistant is inherently 1:1** — `observe.py` watches one browser; multi-user assist is
  just N assistant instances, each on a different learner's CDP endpoint. Scales by replication,
  not shared-resource surgery — the least problematic piece.

## The honest read
The panel/identity layer is small. Scenario-templating + grading-namespacing is a bounded,
mechanical refactor. The genuinely hard, expensive parts are **physical**: per-user guests (VM
capacity + provisioning-as-code + sensor licenses) and the single shared **CID** (one CID, N
users, namespaced-and-garbage-collected). Multi-user is less a coding problem than a
**resource-multiplication** problem — and the two things you multiply (Proxmox capacity, Falcon
CIDs) are exactly the two you do not own cheaply in multiples.

If it is ever pursued, the use case picks the model:
- **Instructor-led class** → Model 3 (time-shared guests) + per-user console namespacing: cheap,
  mostly-simultaneous, minimal infra.
- **Independent self-service learners** → Model 1 isolation, which is really a *cloud-lab
  product* with all the provisioning, capacity, and licensing that implies.
