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
| Proxmox root key | yes (manages guests) | **no** — it has no guests | — |
| Falcon API client | CCFA's, read-only | **its own** | — |

The CCSE runner refuses the CCFA guests (VMs 900–902) in `lab/config.py`, so a scenario with
`target: lnx|win` errors rather than reverting a CCFA VM. The collector scenarios wait for a CCSE
VM of its own.

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

1. **Tunnel.** Zero Trust > Networks > Tunnels > create `ccse-lab` (cloudflared). Install
   `cloudflared` in 904 from Cloudflare's apt repository and run the `cloudflared service install
   <token>` command the dashboard gives. Do not add public hostnames yet.
2. **Access applications, before any DNS.** Self-hosted applications for `ccselab.frame10.com`
   and `labs.frame10.com`, with the same policy as `falconlab.frame10.com`
   (`tenpin.cloudflareaccess.com`).
3. **Public hostnames on the tunnel.** `ccselab.frame10.com` → `http://127.0.0.1:8902`;
   `labs.frame10.com` → `http://127.0.0.1:8903`.
4. **Verify from an unauthenticated client** that each returns a `302` to the Access login:
   `curl -sI https://ccselab.frame10.com/`, `curl -sI https://ccselab.frame10.com/api/term/lnx`,
   `curl -sI https://labs.frame10.com/`. A `200` on any of them means stop and fix Access first.

Nothing in these steps touches the CCFA tunnel, its Access application or LXC 903.

## Deploying a change

Runner: push `ccse-lab`, then
`ssh proxmox-1 'pct exec 904 -- git -C /opt/ccse-lab pull --ff-only'` and
`ssh proxmox-1 'pct exec 904 -- systemctl restart ccse-lab.service'` (or re-run `provision.sh`).
Scenarios: `~/ccse/tools/publish_labs.sh`. Landing page: re-run `provision.sh`.
