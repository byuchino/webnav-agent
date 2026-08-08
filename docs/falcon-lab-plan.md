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

## Build log: what exists (2026-08-08)

**Network — done and verified.** `vmbr1` at `10.77.0.1/24` on proxmox-1, defined in its own
`/etc/network/interfaces.d/vmbr1-falcon-lab` so the file defining `vmbr0` was never touched.
Brought up with `ifup vmbr1` alone, never a full `ifreload`. Verified from *inside* a lab VM:

```
blocked  192.168.254.27:22 / :8006 / :111     (the hypervisor itself)
blocked  192.168.254.1:22, .26:22, .34:22     (LAN)
blocked  10.77.0.1:22 / :8006                 (gateway side)
https: 200,  dns ok,  gateway ping ok
```

> Two isolation gaps found by testing from the guest rather than trusting the ruleset:
> - **FORWARD rules do not cover the hypervisor.** Traffic to `192.168.254.27` is destined *to*
>   the host, so it traverses `INPUT`, not `FORWARD`. The lab could reach Proxmox SSH and the
>   web UI until explicit `INPUT` rules were added (ICMP allowed, everything else rejected).
> - **The management host needs a route**: `ip route add 10.77.0.0/24 via 192.168.254.27`.
>   **Not persistent** — re-add after a reboot of the management box.

**VM 901 `falcon-lab-lnx` — done.** Ubuntu 24.04 cloud image + cloud-init, SSH-ready at
`10.77.0.11` as `labadmin` with `~/.ssh/falcon_lab_ed25519`. Snapshot `clean-cloudinit` taken.
Fully hands-off to rebuild.

**VM 900 `falcon-lab-win` — OS installs unattended; first-boot config does NOT run.**
Windows 11 Enterprise LTSC 2024 installs with zero interaction: disk partitioned, edition
selected, `labadmin` created, auto-logon to desktop. What does not happen is
`FirstLogonCommands`, so the machine has no IP and no SSH, and is currently reachable only
through the Proxmox console.

### Windows lessons, each of which cost a cycle

1. **UEFI media waits for a keypress.** *"Press any key to boot from CD or DVD"* is not
   skippable from the answer file; with a blank disk it then reports "No bootable option".
   Drive it with `qm sendkey 900 ret` in a loop for the first ~30s of boot. Later boots must
   NOT get keys, or Setup restarts in a loop.
2. **virtio needs drivers in the installed OS, not just WinPE.** `drvload` in a
   `RunSynchronous` makes the disk visible to Setup — the image applies fine — and then
   Windows bluescreens with `INACCESSIBLE_BOOT_DEVICE` on first boot, because the driver was
   never added to the target. `Microsoft-Windows-PnpCustomizationsWinPE/DriverPaths` does add
   it, but a `DriverPath` that does not exist is a hard error and the CD letter is unknowable
   in WinPE — that route fails with `0x80070002`. **Resolution: `sata0` + `e1000`, both
   natively supported.** A detection lab does not care about disk throughput.
3. **Select the image explicitly.** This media has two editions and no `ei.cfg`; index 1 is
   LTSC 2024, index 2 is the N edition. Read the names out of `install.wim` rather than
   guessing (there is a WIM XML parser inline in the session history).
4. **Drive letters in the installed OS**: `C:` system, `D:` Windows ISO, `E:` virtio,
   `F:` UNATTEND.
5. **A fresh install resets the evaluation clock** — the first install showed "License is
   expired" (the 2024 image is past its built-in expiry), the reinstall showed
   "valid for 90 days".
6. `tools/qm_type.py` types into a VM console via `qm sendkey`, for when a guest has no
   network and the virtual keyboard is the only way in.

### The open bug and the fix to try first

`FirstLogonCommands` never runs. Ruled out: the script is present and reachable
(`Test-Path F:\lab\setup.ps1` -> True) and it never started (`C:\lab-setup.log` -> False, and
the transcript is its first statement). Removing the deprecated `SkipMachineOOBE`/
`SkipUserOOBE` did not fix it and re-introduced the OOBE region prompts, which need a
`Microsoft-Windows-International-Core` component in the **oobeSystem** pass (only the
`-WinPE` variant was present, in `windowsPE`).

**Do not keep debugging this through the console.** The durable fix is to stop making
reachability depend on a first-logon script at all: run **DHCP on `vmbr1`** (dnsmasq on
proxmox-1, or a Proxmox SDN simple zone). Then Windows gets an address from its default
configuration, SSH can be installed remotely, and `setup.ps1` becomes a convenience rather
than a single point of failure. That also removes the static-IP step from the Linux side.

Note also that manual console attempts failed with `Windows System Error 5` because a
Start-menu PowerShell is **not elevated**; `FirstLogonCommands` would have run elevated.

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
