# CrowdStrike Falcon learning lab — plan and state

**Status:** planned, not yet built. Last worked 2026-08-08.

The goal the web-navigation agent actually serves: an on-demand virtual lab for learning
CrowdStrike Falcon. A small number of VMs with sensors registered to a real CID, then
scenarios (EICAR, Atomic Red Team) with interactive walkthroughs of detection and
troubleshooting in the console. The operator follows along in their own CID and asks for
correction when stuck.

## Decisions taken

| Decision | Choice | Note |
|---|---|---|
| Platform | Proxmox | `proxmox-1` at 192.168.254.27 |
| Guests | Windows + Linux | Win11 Ent LTSC 24H2 and Ubuntu 24.04 |
| CID | Production, dedicated host group | Lab hosts get their own policies |
| Falcon access | **Browser agent only** | Operator's explicit choice, against the recommendation below |

### The recommendation that was declined, recorded honestly

A read-only Falcon API client was advised for checking tenant state: structured JSON instead
of scraped DOM, read-only enforced at the credential rather than by model judgement, no
browser session to hijack, and auditable access. The operator chose browser-agent-only.

That is a legitimate call, but it has one consequence that shapes the build order: **every
claim about tenant state now rests on a 4B model with no independent check.** The same agent
has already been observed reporting CrowdStrike's Mexican listing in pesos as the NASDAQ share
price — correct extraction from the wrong instrument, with every signal reporting success. In a
lab whose whole purpose is learning to read detections correctly, that is the failure mode that
matters most. Hence the verification layer is a prerequisite, not a roadmap item.

## Host survey (2026-08-08)

**proxmox-1** — PVE 8.4.6, 16 cores, 62 GB RAM (~26 GB free). Use the `vms` lvmthin pool
(616 GB free); **avoid `local-lvm`, it is 87 % full**. proxmox-2 is too small (4 cores, 7 GB
free) for Windows guests.

Media already present on the DS923 NFS share — nothing to download, and both Windows images
are 90-day evaluations so there is no licensing question:

- `26100.1742...CLIENT_LTSC_EVAL_x64FRE_en-us.iso` — Win11 Enterprise LTSC 24H2
- `26200.6584...CLIENTENTERPRISEEVAL...iso` — Win11 Enterprise Eval 25H2
- `ubuntu-24.04.3-desktop-amd64.iso`
- `virtio-win-0.1.285.iso` — required for Windows on Proxmox

## Network isolation

Atomic Red Team simulates credential access and lateral movement, so lab VMs must not reach
the rest of the LAN. They *must* still reach the internet, because the sensor has to talk to
the Falcon cloud — so this is egress-only isolation, not an air gap.

> **Do not enable the Proxmox datacenter firewall.** Eight existing VMs on proxmox-1 carry
> `firewall=1` on their NICs with no datacenter firewall configured, so the rules are currently
> inert. Enabling it at datacenter level would immediately start enforcing default-deny on all
> of them and would likely break Handbrake, Ubuntu-Docker and the WireGuard gateway.

Use a dedicated internal bridge instead, which touches nothing that already exists:

- `vmbr1` on proxmox-1, `10.77.0.1/24`, no physical port
- MASQUERADE `10.77.0.0/24` out of `vmbr0`
- FORWARD: DROP `10.77.0.0/24` → `192.168.254.0/24`
- FORWARD: ACCEPT `10.77.0.0/24` → everything else (internet)
- FORWARD: ACCEPT `192.168.254.34` → `10.77.0.0/24` (management host)
- Persist the rules so they survive a host reboot

## VMs to create

| VMID | Name | Spec |
|---|---|---|
| 900 | `falcon-lab-win` | Win11 Ent LTSC 24H2, OVMF + TPM 2.0, 6 GB, 4 cores, 64 GB, virtio-scsi + virtio-win |
| 901 | `falcon-lab-lnx` | Ubuntu 24.04, 4 GB, 2 cores, 40 GB |

Snapshot both at **"clean, sensor installed, pre-scenario"**. Revert-after-scenario is most of
what makes a lab worth having — every exercise must start from an identical state.

## Scenarios, in order

1. **EICAR** — proves the whole pipeline end to end before anything subtle.
2. **CrowdStrike's documented sensor test** — confirms the sensor reports as expected.
3. **Atomic Red Team**, by ATT&CK technique.

Atomics over live malware is a pedagogical choice as much as a safety one: each atomic has a
documented expected outcome, so there is a right answer to check an investigation against. Live
samples give an unreproducible mess where a missed detection is indistinguishable from a sample
that never detonated — and they would pollute a production detection queue.

## Blockers

1. **CCID and sensor installers** (Windows `.exe`, Ubuntu `.deb`) from Host setup and
   management → Sensor downloads. Headless CDP cannot reliably pull files, so this is a manual
   download regardless of the browser-only decision.
2. **Falcon session in the dedicated profile** — sign in headed on the Windows box with
   `--user-data-dir=C:\cdp-profile`, complete MFA, close it. See the README's remote-browser
   section.
3. **Go/no-go on infrastructure** — creating `vmbr1`, the iptables rules, and two VMs.

## Build order

1. Verification layer (guide §16.4) — prerequisite, see above.
2. Page-as-document retrieval (guide §9) — the docs portal is long-form reading, and without
   it the agent scroll-loops instead of extracting.
3. `vmbr1` + isolation rules.
4. The two VMs, then sensors, then the clean snapshot.
5. Falcon host group and a detection-only prevention policy.
6. Scenario 1 (EICAR) end to end.
