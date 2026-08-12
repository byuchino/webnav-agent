# Deployment — where the panel actually runs

Written because a dropped session cost an hour rediscovering it. The panel does **not** run on
the dev box. If you are looking for a process on your laptop, you will not find one.

## The short version

| | |
|---|---|
| Host | Proxmox **LXC 903 `falcon-lab-controller`** on `Envy` (SSH alias `proxmox-1`, 192.168.254.27) |
| Code | `/opt/falcon-lab` — a git clone tracking `origin/main` |
| Panel | `falcon-lab.service` → uvicorn on **127.0.0.1:8901** (loopback only) |
| Tunnel | `cloudflared.service` → tunnel `falcon-lab`, token at `/etc/cloudflared/token` |
| Public URL | `https://falconlab.frame10.com`, behind Cloudflare Access (`tenpin.cloudflareaccess.com`) |

The controller is dual-homed: `eth0` on the LAN (Proxmox API, internet, cloudflared) and `eth1`
on the lab subnet `10.77.0.0/24` (the guests). That is why it can grade and open shells while
the dev box cannot — the dev box has **no route to `10.77.0.0/24`**, so `lab.py` run there will
report guests unreachable. This is normal, not a fault.

## Reaching it

Everything goes through the Proxmox host; the container is not directly reachable from the dev
box's network path.

```
ssh proxmox-1 'pct exec 903 -- <command>'
```

Useful ones:

```
ssh proxmox-1 'pct exec 903 -- systemctl status falcon-lab.service'
ssh proxmox-1 'pct exec 903 -- systemctl status cloudflared.service'
ssh proxmox-1 'pct exec 903 -- curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8901/'
```

Quoting nests three deep (local shell → ssh → `pct exec` → container shell). Parentheses and
`@{...}` in an inner `echo` will break it; keep inner commands plain, or push a script.

## Deploying a change

```
ssh proxmox-1 'pct exec 903 -- git -C /opt/falcon-lab pull --ff-only'
ssh proxmox-1 'pct exec 903 -- systemctl restart falcon-lab.service'
```

Push to `origin/main` first — the clone pulls from GitHub, not from the dev box.

The remote is **anonymous HTTPS**, which works because `byuchino/webnav-agent` is public (a
deliberate choice, revisited and kept on 2026-08-12). If the repo is ever made private this
breaks: add `/root/.ssh/id_ed25519.pub` (already present in the container, currently authorising
nothing) as a read-only **deploy key** and switch the remote to SSH.

Until 2026-08-12 `/opt/falcon-lab` was a plain file copy with no version marker, and it silently
drifted two commits behind while in use. That is why it is a clone now: "is prod current?" should
be answerable with `git status`.

## What lives on the controller

Secrets, all `0600`/`0700`, none of them in the repo:

- `/root/.ssh/falcon_lab_ed25519` — the key that reaches the guests
- `/root/.ssh/id_ed25519` — authorised as root on the Proxmox host, for `qm`/`pct` calls
- `/root/.falcon-lab/ccid` — the CID-with-checksum
- `/root/.falcon-lab/api.json` — read-only Falcon API credentials for `kind: api` grading

`LAB_PVE=192.168.254.27` is set in the unit environment rather than in `config.py`, so the repo
stays host-agnostic.

## The bind, and why it is loopback

`falcon-lab.service` calls uvicorn with `--host 127.0.0.1` explicitly. Keep it that way. The
panel has no authentication of its own and `/api/term/{host}` is a shell on the guests, so
"reachable" and "compromised" are the same word. `cloudflared` connects from inside the same
container, so loopback costs nothing.

`./lab.py web` (the CLI path, not used here) defaults to loopback for the same reason and takes
`--host` for the deliberate case.

## Access is not optional, and it is a separate step

The tunnel and the Access application are different objects created in different steps, and
**the tunnel is the half that grants access**. Routing DNS to the tunnel before the Access app
exists publishes an unauthenticated admin shell — that happened on 2026-08-12 and is written up
in `small-team-remote.md`. The order is: tunnel → Access application → DNS → verify with an
unauthenticated client that you get a `302` to the Access login, on `/` **and** on
`/api/term/win`.

## Verifying a deployment

```
ssh proxmox-1 'pct exec 903 -- git -C /opt/falcon-lab log --oneline -1'   # matches origin/main?
ssh proxmox-1 'pct exec 903 -- git -C /opt/falcon-lab status --short'     # empty = no drift
```

Then load `https://falconlab.frame10.com` — Access login, then the panel. `cf-cache-status` is
`DYNAMIC`, so there is no cache to bust; a plain reload shows new UI.
