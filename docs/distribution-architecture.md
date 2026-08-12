# Distribution architecture: splitting the lab from the AI assistant

A design note on packaging the system as two distributable components. Nothing here is built
yet; this records the options and the reasoning so the decision can be made deliberately.

## The two components, and why they separate cleanly

The system is really two things that today live in one repo:

- **The Falcon Lab ecosystem** — `lab/` (config, core, falcon, scenarios, sensor, web) plus
  `scenarios/*.yaml`. Orchestrates Proxmox guests over SSH, serves the browsable control panel,
  runs and grades exercises. Runtime dependencies: `falconpy` (API grading), SSH (Proxmox +
  guests), `fastapi`/`uvicorn` (panel).
- **The AI lab assistant** — `observe.py`, `agent/` (cdp, snapshot, …), `tools/docs_search.py`,
  the docs corpus, and a prompt that teaches an AI its function. Attaches read-only to a browser
  over CDP to watch and correct a learner, and answers from the harvested Falcon docs.

**The coupling today is a single point:** `lab/falcon.py` imports the CDP tools (`cdp`,
`snapshot`, `NetworkRecorder`) for the `kind: console` grading checks. Nothing else in `lab/`
touches the agent.

**The API migration is the decoupling.** Once the remaining `kind: console` checks become
`kind: api` (falconpy) and `falcon.py`'s CDP `read()/check()` are deleted, the lab has **zero**
code dependency on the agent. Dependency direction confirms the split is natural: the assistant
never needed the lab (it observes *any* console); the lab needed the agent only for browser
scraping, which the migration removes.

**Status (2026-08-12): 5 of 7 done, 2 checks hold the coupling open.** `04 lab-host-group`,
`03 rfm-and-inactive-hosts`, `05 rtr-run-binaries`, `05 sensor-update-pinning`, and
`06 ioc-blocklist-hash` are now `kind: api`, and each gained a real assertion in the process —
the sensor-update one immediately exposed a false pass, since the old substring match on the
policies page said nothing about whether the policy was *assigned* to the group. What remains:

- `01 roles-and-least-privilege` and `07 audit-logs-and-reports`. Both are `api_match: /api2/`
  with no `expect_contains` — they assert only that a page rendered, and both files already
  carry `attest` blocks covering the actual learning.
- `07` **has no API equivalent**: falconpy 1.6.4 exposes `KnowledgeBaseAuditEvents` and
  `RealTimeResponseAudit`, neither of which is the console audit log at `/audit-log/
  falcon-console/`. It cannot become `kind: api`; the choice is `attest` or stay coupled.
- `01` *could* use `UserManagement`, but the resulting check would assert that the **grader's**
  API key can read roles — a test of our credentials, not of the learner's work.

Until both are resolved, `falcon.py` keeps importing `cdp`/`snapshot` for `_check_console` and
Component A cannot ship without the agent's browser code. Two checks that verify almost nothing
are the whole of the remaining dependency.

Unrelated but adjacent: `06 ioc-blocklist-hash` grades only once the API key is given the
**IOC Management: Read** scope; without it the check reports "could not look", never a pass.

```
Before:  assistant (CDP) ◄── lab/falcon.py (console checks)   +   shared docs
After:   lab ──(API+SSH)──► Falcon API + Proxmox
         assistant ──(CDP)──► a browser + docs
                 └────── shared: docs corpus only ──────┘
```

Result: **zero shared code, one shared data artifact (the docs).**

## The one real coupling: the docs — and a distribution constraint

The docs are the shared "source of Falcon truth", but `~/falcon-docs` is **CrowdStrike's
copyrighted documentation**. Bundling it in something third parties download is redistribution
that likely is not permitted. Options:

- **D1 — ship the harvester, not the docs.** Each deployer runs `tools/docs_fetch` against
  their own authenticated Falcon access to populate their own `~/falcon-docs`. Shared asset =
  harvester + location convention, not content. Cleanest legally; also means each deployer's
  docs match their own Falcon version. Adds a harvest step to install.
- **D2 — bundle the docs** (submodule or data package). Simplest technically, no per-deploy
  harvest — but it is the redistribution to avoid for third parties. Fine for your own
  multi-machine use.
- **D3 — hybrid.** Distribute a small *derived* artifact you author (distilled notes/summaries
  you can license) as the shared truth; keep the raw corpus local-only. Most work, cleanest to
  distribute.

Whichever is chosen, the shared thing stays small and data-only; it never re-couples the code.

## Component A — the Lab ecosystem

### Packaging form

- **A1 — Python package + installer.** `pip install falcon-lab`; entry points for the `lab` CLI
  and the panel; a systemd unit for the panel. Natural fit — it is Python.
- **A2 — Container (Docker/OCI).** Panel + lab in a container; mount config + SSH keys; reaches
  Proxmox/guests/Falcon over the network. Clean isolation, one-command run — works well now that
  the lab is API+SSH with no browser to containerize.
- **A3 — Appliance.** An LXC/VM template that runs on the Proxmox host itself, pre-wired.
  Heaviest to build, lightest for the deployer.

### The dominant decision: do you ship the VMs?

Today VMs 900/901/902 were hand-built. For real distributability that is the actual barrier:

- **A-infra-1 — bring your own guests.** The deployer creates VMs matching the config; the lab
  just drives them. Lowest effort for the author, highest for the deployer.
- **A-infra-2 — provisioning-as-code.** Ship Terraform-for-Proxmox / Ansible / cloud-init
  templates and a `lab provision` command that *creates* the guests. This is what makes it
  installable by a stranger. Significant new work; the difference between "a product" and "my
  setup, documented."

### Config

Extract everything currently hardcoded in `lab/config.py` (`HOSTS`, `PVE`, network, snapshot/
baseline names) plus the secrets in `~/.falcon-lab/` (ccid, api.json) into one config, e.g.
`~/.falcon-lab/config.yaml`: Proxmox endpoint/node/pool, per-guest vmid/ip/user/key/snapshots,
network (vmbr/subnet/DHCP), Falcon console URL + region + API creds. `lab/config.py` becomes a
loader/validator over that file.

### Portability gotcha

Several scenarios hardcode `FALCON-LAB-WIN`, `FALCON-LAB-LNX`, and the `Falcon Lab` group name.
For distribution these become either config-templated values the YAML references, or documented
required conventions a deployer reproduces. The former is more portable; the latter is less work.

## Component B — the AI assistant

It is not a daemon but a **capability bundle**: the CDP tools (`observe.py`, `agent/cdp.py`,
`agent/snapshot.py`), `docs_search`, the docs (or harvester), and a prompt doc.

- **B1 — Claude-Code-native (first pass).** A directory dropped into a Claude Code workspace: a
  `CLAUDE.md` (the comprehensive prompt — how to attach read-only via observe.py, Falcon-domain
  pointers, the assist workflow), the tools as scripts, and a config file. Cheapest; tightly
  Claude-Code-shaped.
- **B2 — MCP server (the "any AI" form).** Wrap the capabilities as an MCP server exposing tools
  like `observe_console`, `read_panel_state`, `search_docs`. Any MCP-capable assistant uses it;
  the prompt doc becomes tool descriptions + a portable system prompt. The real answer to "usable
  with any AI"; more work; future-proofs past Claude Code.
- **B3 — both.** Ship B2 and a thin `CLAUDE.md` that points Claude Code at the MCP server.

### Config the AI reads

CDP endpoint (port/tunnel to the browser), docs location, Falcon console URL/region, and
optionally the lab panel URL — so the assistant is **lab-aware without being lab-dependent**: it
reads `/api/scenarios` and grade output over HTTP and never imports lab code. That HTTP seam is
how the two stay decoupled yet cooperative.

## Repo / layout options

- **R1 — two repos.** `byuchino/falcon-lab` (exists, empty) = Component A; `webnav-agent`
  becomes Component B after the lab moves out. Clean ownership, two release cadences.
- **R2 — monorepo, two packages.** One repo with `packages/lab` + `packages/assistant` +
  `shared/docs-tooling`, each independently installable. Easiest to keep shared tooling in sync.
- **R3 — three repos:** lab, assistant, and a shared docs-tooling dependency. Most correct, most
  moving parts.

## Recommended shape

**R1 (two repos)** + **D1 (ship the harvester, harvest-per-deployer)** + **A1 or A2** for the lab

+ **B1 now / B2 next** for the assistant. The two-repo split matches the clean code boundary the
  migration creates; harvest-per-deployer sidesteps copyright and version-matches each deployer;
  the lab as a package/container is low friction; and starting the assistant as a Claude Code
  bundle with an MCP server as the stated next step delivers value now without a Claude-only dead
  end.

**Settle the VM-provisioning fork (A-infra-1 vs A-infra-2) first** — it determines whether
"installable" means "a stranger runs one command" or "a stranger follows a setup guide," and it
shapes everything downstream.
