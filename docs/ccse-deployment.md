# CCSE lab deployment — and how it stays out of the CCFA lab's way

The CCSE (Next-Gen SIEM) lab runs the same runner as the CCFA lab, from this repo's
**`ccse-lab` branch**, in its **own container**. The CCFA lab (`main`, LXC 903,
`falconlab.frame10.com`) is in use for CCFA training and must not be disturbed by CCSE work:
no commits to `main`, no restarts on 903, nothing on its guests or its lab subnet.

| | CCFA lab | CCSE lab | Landing page |
|---|---|---|---|
| URL | `falconlab.frame10.com` | `ccselab.frame10.com` | `labs.frame10.com` |
| Container | LXC 903 `falcon-lab-controller` | **LXC 904 `ccse-lab-controller`** | LXC 904 |
| Code | `/opt/falcon-lab`, `main` | `/opt/ccse-lab`, `ccse-lab` | `/srv/labs-landing` |
| Service | `falcon-lab.service`, 127.0.0.1:8901 | `ccse-lab.service`, 127.0.0.1:8902 | `labs-landing.service`, 127.0.0.1:8903 |
| Scenarios | `scenarios/` in this repo | `/opt/ccse-labs`, pushed from the private `~/ccse` repo | — |
| Networks | LAN + lab subnet `vmbr1` (10.77.0.0/24) | **LAN only** | — |
| Proxmox access | root SSH key (manages guests) | **API token scoped to VM 905** | — |
| Guests | VMs 900, 901 (vmbr1) | **VM 905 `ccse-lab-lnx`** (vmbr0, no sensor) | — |
| Falcon API client | CCFA's, read-only | **its own** | — |

The CCSE runner refuses the CCFA guests (VMs 900–902) in `lab/config.py`; `target: lnx` is
the CCSE guest, VM 905.

## The CCSE guest and its token (2026-10-17)

VM 905 is a full clone of template 998 (Ubuntu 24.04): 2 vCPU, 4 GB, `vmbr0` DHCP, cloud-init
user `labadmin` with only the two `ccse_lab_ed25519` public keys (904's `/root/.ssh/` and the dev
box's `~/.ssh/`), template password removed, no Falcon sensor. Snapshot `bare` was taken after a
clean shutdown. Rebuild a baseline as root on proxmox-1: `qm shutdown 905 && qm snapshot 905 bare`
(delete the old one first); the runner's token cannot.

The token: role `CCSELabGuest` (`VM.Audit,VM.PowerMgmt,VM.Snapshot.Rollback,VM.Monitor`), user
`ccse-lab@pve`, ACL on `/vms/905` only, token `runner` (privsep 0). Its secret was written by
`pveum user token add … --output-format json` piped straight into
`/root/.falcon-lab/pve-token.json` (0600) on 904, beside `/etc/pve/pve-root-ca.pem` so TLS is
verified. Tested from 904: 200 on 905's status, snapshots and agent; 403 on VMs 900, 901, 903 and
300, on creating a snapshot and on changing config. Revoke with
`pveum user token remove ccse-lab@pve runner`.

## Provisioning 904 (once)

The container was created 2026-10-16 (Debian 12, 2 cores, 2 GB, 8 GB on `vms`, `vmbr0` DHCP,
unprivileged, nesting, onboot). Install the runner and both services:

```
ssh proxmox-1 'pct exec 904 -- sh' < ~/ccse-lab-runner/deploy/ccse/provision.sh
```

A fresh Debian 12 container has no `curl`, so the script is fed from the dev box's checkout
rather than fetched; it installs `curl` and `git` itself, then clones the branch.

`deploy/ccse/provision.sh` is idempotent: it clones (or pulls) `ccse-lab` into `/opt/ccse-lab`,
builds the venv, installs `ccse-lab.service` and `labs-landing.service`, publishes the landing
page from `deploy/ccse/landing/`, and prints the two loopback status codes plus any TODO.

Then, from the dev box: `~/ccse/tools/publish_labs.sh` (copies `labs/` in and verifies by md5 on
the container; scenarios are read per request, so no restart).

## The Falcon API client

Create a **separate** API client for the CCSE lab in the Falcon console (not CCFA's). Grading
starts NG-SIEM query jobs, which the API files under **NGSIEM: WRITE** even for a read-only
search; the runner stops each job when done and writes nothing else. Put it at
`/root/.falcon-lab/api.json`, mode `0600`, as `{"client_id": "...", "client_secret": "...",
"cloud": "us-2"}`. Without it, API checks report "could not look", never a pass.

## Cloudflare — the order matters

The tunnel is the half that grants access. On 2026-08-12 DNS was routed to the CCFA tunnel before
its Access application existed and an unauthenticated admin shell was public for ~17 minutes
(`small-team-remote.md`). So:

Done 2026-10-17, in this order. The dashboard's menus had moved since the CCFA lab was set up:
Tunnels is its own sidebar section (**Networking > Tunnels**), and hostnames on a tunnel are now
**routes**.

1. **Tunnel.** Networking > Tunnels > Create Tunnel, name `ccse-lab`, OS Debian, 64-bit.
   Install the package with
   `ssh proxmox-1 'pct exec 904 -- sh' < ~/ccse-lab-runner/deploy/ccse/install-cloudflared.sh`,
   then inside 904 (`ssh -t proxmox-1 'pct exec 904 -- bash'`) run
   `cloudflared service install <token>` by hand, so the token is in no file or history. Continue
   returns to the tunnel list without asking for a route: `ccse-lab` Healthy, no routes.
2. **Access, before any route.** Zero Trust > Access controls > **Policies**: `CCSELabLearners`
   (Allow; Include > Emails: the owner only; session same as the application). Then
   Access controls > **Applications** > Self-hosted, one per hostname, each with only
   `CCSELabLearners`: `ccselab` (`ccselab.frame10.com`) and `labs` (`labs.frame10.com`), all
   identity providers, 24 h. CCFA's `FalconLabLearners` is **not** reused: it would let CCFA
   learners into a panel whose grader searches the tenant. Widen `CCSELabLearners` to add people.
3. **Routes.** On the `ccse-lab` tunnel: Add route > Published application.
   `ccselab.frame10.com` → `http://127.0.0.1:8902`; `labs.frame10.com` →
   `http://127.0.0.1:8903`. DNS is created automatically.
4. **Verify from an unauthenticated client** that each returns a `302` to the Access login
   (`tenpin.cloudflareaccess.com`): `curl -sI https://ccselab.frame10.com/`,
   `curl -sI https://ccselab.frame10.com/api/term/lnx`, `curl -sI https://labs.frame10.com/`.
   A `200` on any of them means stop and fix Access first. All three were 302 on 2026-10-17.

Nothing in these steps touches the CCFA tunnel, its Access application or LXC 903.

## Deploying a change

Runner: push `ccse-lab`, then
`ssh proxmox-1 'pct exec 904 -- git -C /opt/ccse-lab pull --ff-only'` and
`ssh proxmox-1 'pct exec 904 -- systemctl restart ccse-lab.service'` (or re-run `provision.sh`).
Scenarios: `~/ccse/tools/publish_labs.sh`. Landing page: re-run `provision.sh`.
