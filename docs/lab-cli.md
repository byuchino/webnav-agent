# Lab CLI and control panel

Driving the lab through an assistant costs a conversation every time, and a scenario run is a
fixed sequence that does not need one. This exists so routine work costs nothing: one command,
a few aligned lines of output, no round trip through a model.

```
./lab.py status                      # hosts, reachability, sensor state
./lab.py scenarios                   # what is available
./lab.py show sensor-install-win     # full text of one exercise
./lab.py run  sensor-install-win     # prepare it
./lab.py grade sensor-install-win    # check your work
./lab.py revert win bare             # roll back, seconds not minutes
./lab.py web                         # control panel on :8901
```

## Two baselines, and why

Every guest has two snapshots, and every scenario declares which one it needs:

| Baseline | Snapshot | For |
|---|---|---|
| `bare` | `clean-preSensor` / `clean-cloudinit` | the sensor-installation exercises |
| `sensor` | `clean-withSensor` | everything else |
| `rfm` (lnx) | `clean-rfm` | the Reduced Functionality Mode exercise |

The `rfm` baseline exists so that "put a host in RFM" is a button rather than an afternoon.
Constructing it was not obvious: **booting an unsupported kernel is not enough.** A current
Linux sensor on `--backend=auto` falls back to eBPF user mode and stays perfectly healthy —
tested here on a 7.0 kernel against a 7.37 sensor, which reported `rfm-state=false`. It takes
both an unsupported kernel *and* `falconctl -s --backend=kernel` to remove the user-mode
escape route, at which point the reason becomes explicit:

```
rfm-state=true.
rfm-reason=Modules file was not found, code=0xC0000034.
```

**Why there is no Windows RFM baseline.** RFM applies on Windows too, but it could not be
induced here the way Linux was, and the investigation is worth recording so it is not repeated:

- The only cause the harvested docs document is an **unsupported OS build** (Windows Insider /
  beta), which is not practical to stage in the lab.
- **Stopping a CrowdStrike component does not work** and would not be RFM anyway. Even as an
  administrator, `sc stop CSFalconService` returns `Access is denied` and the kernel drivers
  (`csagent`, `CSDeviceControl`, `CSFirmwareAnalysis`) return `1052 — control not valid`: that
  is sensor tamper protection. A stopped sensor is *not running*, a different state from RFM,
  which is a sensor that **is** running and reporting in while collecting almost nothing.
- The four OS services the sensor requires (BFE, NSI, LMHosts, Power) are **communication /
  functionality prerequisites, not RFM triggers**. Of the four, only `lmhosts` can be stopped
  in isolation — `nsi` cascades into DHCP/DNS (it would drop the ssh session), `BFE` cascades
  into the Windows Firewall stack, and `Power` cannot be stopped at all. Stopping `lmhosts` was
  tested: the sensor kept running, forced nothing back on, and logged no RFM/degraded event.
  Stopping these degrades connectivity (host shows **offline / stale**), which is a separate
  teaching topic from RFM.

The short version: on both platforms RFM is a **kernel-driver-load-time** state, which is why
Linux needed a kernel/module mismatch to reach it and why stopping a user-space dependency on
Windows does not. There is also no local Windows RFM read — unlike `falconctl -g --rfm-state`,
`CSSensorSettings.exe` exposes only proxy/tags/rtr.

> **Correction, 2026-08-12: Windows RFM induced itself, for free.** The conclusion above —
> "not practical to stage in the lab" — was wrong about the *effort*, right about the mechanism.
> `FALCON-LAB-WIN` is now reporting `reduced_functionality_mode: yes` with nobody having tried
> to put it there. The evidence lines up exactly with the documented cause:
>
> | | |
> |---|---|
> | Sensor | 7.40.21306.0 (N-2, pinned deliberately) |
> | OS build | Windows 11 26100, **UBR 9168** |
> | Last updates | KB5121003 (8/12), KB5123304 + KB5120710 (8/11) |
> | Enrolled healthy | 8/09 — the `clean-withSensor` baseline was built from a non-RFM host |
>
> A Windows cumulative update walked the kernel build past what the pinned N-2 sensor supports,
> which is the same kernel/module mismatch Linux needed a hand-built kernel to reach. Running a
> deliberately old sensor on an OS that patches itself **is** the staging mechanism.
>
> Note what this costs the naive checks: `sc query csagent` reports the driver **RUNNING** on
> this host right now, and `CSFalconService` is Running too. Every host-side signal looks
> healthy while the sensor collects almost nothing — which is the entire point of the RFM
> teaching topic, now demonstrable on Windows rather than merely described.
>
> **A `win` `rfm` baseline is therefore available for the first time** — the guest is sitting in
> the state, so snapshotting it captures what could not previously be built. It is perishable:
> reverting `win` to `clean-withSensor`, or updating the sensor, throws the state away.

**RFM detection is server-side and platform-agnostic.** Confirmed against CrowdStrike's own SDK
(`~/falconpy`, `samples/hosts/rfm_report.py`): every device record carries a
`reduced_functionality_mode` field (value `yes` / `no` / `unknown`), and it is an FQL filter key
on `QueryDevicesByFilter`. The console's RFM column and its RFM filter are exactly
`reduced_functionality_mode:'yes'` — the same field for Windows, Linux and macOS. So Windows RFM
is *detected* the same way Linux is (from what the sensor reports to the cloud); what differs is
only that Linux additionally lets you read the state on the box. The `induce-rfm` exercise sends
you to Host Management's RFM filter, which is that FQL query.

The runner reverts to the declared baseline before doing anything, so an exercise cannot
silently begin from the wrong state — a sensor-install lesson that started on a host which
already had one would be quietly pointless. Ask for a baseline that does not exist and it
refuses with the command to build it:

```
error: win: no 'clean-withSensor' snapshot yet (have: clean-preSensor)
  Build it once:  ./lab.py sensor install win --ccid <CID>
                  ./lab.py snapshot win clean-withSensor -d 'sensor registered'
```

## Snapshots must be taken with the guest shut down

`./lab.py snapshot` shuts the guest down first, and that default is not caution — it is the
difference between a rollback point and a corrupt one.

Proxmox without `--vmstate` captures the disk only. Taken while Windows has writes in flight,
the result is crash-consistent at best. The first `clean-withSensor` snapshot here was taken
live, and its rollback would not boot:

```
Recovery -- Your PC/Device needs to be repaired
\WINDOWS\System32\drivers\WindowsTrustedRTProxy.sys   error 0xc0000225
```

A critical driver caught mid-write. Worse, rolling back *applied* that image, so the working
install was gone until an earlier snapshot rescued it. Linux survived identical treatment
because ext4 journals; NTFS did not — so "it worked on the Linux guest" is not evidence.

`--live` exists for when you want speed and accept the risk. Do not use it for a baseline.

## Sensor readings are stale immediately after a revert

`wait_ready` returns as soon as ssh answers, but the sensor needs another 30–45 seconds to
initialise, and until it does `falconctl` reports the **pre-boot** state. Reverting from `rfm`
to `sensor` and checking straight away reports `rfm-state=true` on a host that is fine — a
stale reading indistinguishable from a failed revert. `prepare()` therefore waits for the
sensor to settle whenever the baseline involves one, and says so in the progress log.

## Manual scenarios

Installing a sensor is on the CCFA syllabus, so it is an exercise rather than setup. Those
scenarios are `mode: manual`: the runner reverts to a sensor-free host, prints what to do, and
**stops**. You do the work; `grade` tells you objectively whether it took.

That separation is why `sensor install` and `sensor verify` are different commands. `install`
exists only to build the `sensor` baseline for other scenarios. `verify` never installs
anything — it reports what is actually true on the host.

Grading checks the whole chain, not just that a service is running:

```
sensor-install-lnx     NOT YET  sensor NOT installed
Work down the chain, because each step can fail independently:
  dpkg -l falcon-sensor                          package present?
  sudo /opt/CrowdStrike/falconctl -g --cid       CID set?
  systemctl status falcon-sensor                 service running?
  sudo /opt/CrowdStrike/falconctl -g --aid       registered with the cloud?
  sudo /opt/CrowdStrike/falconctl -g --rfm-state should be false
```

**RFM counts as a failure.** Reduced Functionality Mode is where a sensor lands without a
matching kernel module: it installs, it runs, it registers, and it collects almost nothing. It
passes every naive check, which is precisely why the grader fails it — that distinction is the
lesson. Likewise a sensor installed with no CID gives a running service and an AID that never
arrives, which is the most common real deployment mistake and worth doing on purpose once.

## Adding a scenario

Drop a YAML file under `scenarios/<domain>/`. No code. Scenarios are organised by the eight
CCFA exam domains, plus `00-foundations` for the pipeline smoke test.

```yaml
id: my-scenario
name: Human readable name
target: win | lnx | console   # console = no guest at all
baseline: sensor              # bare | sensor; omit for console scenarios
mode: auto | manual | guided
domain: 5                     # CCFA domain number
objective: "5.1 — ..."        # the exam objective this serves
difficulty: 2
requires: [lab-host-group]    # advisory, reported not enforced

summary: one line
teaches: |
  what the operator should walk away knowing
setup:                        # guided mode: runner does this, then hands over
  - name: prepare
    shell: ...
steps:                        # auto mode: runner does all of it
  - name: do the thing
    shell: ...
    expect_contains: optional
instructions: |               # manual and guided: what YOU do
expect:
  console: where to look in Falcon
verify:                       # a LIST — a scenario may need several
  - kind: console
    label: policy is assigned
    path: /prevention-policies
    api_match: /api/
    expect_contains: "Falcon Lab"
  - kind: endpoint
    label: behaviour actually changed
    shell: ...
    expect_contains: removed
  - kind: sensor
    label: sensor health
  - kind: attest
    label: understanding
    items: ["...", "..."]
hint: shown only when not passing
```

### Three targets, because most of CCFA never touches an endpoint

Creating roles, setting policy precedence, adding an IOC, building a workflow — none of it
involves a guest. `target: console` scenarios have no host and no baseline to revert to.

### Verification is a list

`configured` and `functional` are different claims, and the gap between them is where policy
precedence, group assignment and propagation delay actually live. A console check says the
setting was saved; an endpoint check says the behaviour changed. The policy exercise requires
both, and when the console passes while the endpoint fails, that combination *is* the lesson.

Console paths and the API prefix were **discovered, not guessed**. The first version used
`/api/` and plausible-looking paths like `/host-groups`; six of seven returned "Error |
CrowdStrike" and the one that loaded captured zero bodies. The console publishes its own route
manifest at `/content/sitemap-v2/index.json` — 552 routes — and its API lives under `/api2/`.
Real paths for this tenant:

| Domain | Path |
|---|---|
| Host groups | `/host-management/host-groups` |
| Hosts | `/host-management/hosts` |
| Prevention policies | `/policies/prevention` |
| Sensor update policies | `/policies/sensor-update` |
| Custom IOCs | `/iocs` |
| Roles | `/users-v2/roles-and-permissions` |
| Audit logs | `/audit-log/falcon-console/`, `/audit-log/api`, `/audit-log/prevention-policy` |
| API clients | `/api-clients-and-keys` |
| Quarantined files | `/activity/quarantined-files` |
| ML exclusions | `/configuration-v2/exclusions/machine-learning` |

If a check reports "no API response matching", read the sitemap for your own tenant before
adjusting anything else — routes differ by subscription.

Console checks read the API response the console itself received — not the DOM. Substring
matching rather than a JSON path language, deliberately: the console's internal API is
undocumented and unversioned, so its response *shape* is the least stable thing about it,
while a group's name appearing in the payload is durable.

### `None` is never a pass

A check that could not run — expired console session, unreachable guest — returns `None`, and
a scenario whose checks all returned `None` grades as **UNVERIFIED**, not passed:

```
lab-host-group               UNVERIFIED
  --   Falcon Lab group exists    the console session has expired — sign in again
  --   understanding              self-checked (not verified automatically)
        [ ] Both lab hosts are members of the group
```

"I could not look" and "I looked and it was wrong" are different answers, and a grader that
conflates them is worse than no grader. `attest` items are shown as an honest checklist and
never counted as verification.

## Reading console state: a read-only Falcon API key (`kind: api`)

`console` checks scrape the browser's own API traffic over CDP. That works for simple presence
assertions, but it breaks on anything relational — a policy references a host group by **ID**,
so the group *name* a check wants is never in the scraped payload. Rather than reverse-engineer
the console's undocumented internal API, the grader reads config state from the **documented
Falcon API** via `falconpy`, behind the `lab/falcon.py` seam. It is **read-only and optional**:
with no key configured, `api` checks return `None` ("could not look"), never a false failure.

**Credentials** (`config.api_creds()`), env wins over the file:

```
export FALCON_CLIENT_ID=... FALCON_CLIENT_SECRET=... FALCON_CLOUD=us-2
# or a 0600 file (keeps the secret out of shell history):
printf '{"client_id":"...","client_secret":"...","cloud":"us-2"}' > ~/.falcon-lab/api.json
chmod 600 ~/.falcon-lab/api.json
```

**Create the API client** in the console (Support and resources → API clients and keys) with
only these **read** scopes — it cannot change anything:

| Scope (exact console label) | Used by |
|---|---|
| Host Groups | group-name→id resolution (all `policy_group` checks) |
| Hosts | `host_count` (duplicate/stale-host dedup), `hosts_matching`, `rfm_state` |
| Response / Prevention / Sensor update Policies | `policy_group` per type |
| Custom IOA Rules | `ioa_group_enabled` |
| Alerts | `detection_contains` (IOA fired) |
| Machine Learning Exclusions | `ml_exclusion` |
| Quarantined Files | `quarantined_file` |
| IOC Management | `ioc_exists` (added 2026-08-12; without it the check reports "could not look") |

**`kind: api` asserts** (in a scenario's `verify` list):

```yaml
- kind: api
  assert: policy_group        # policy: response|prevention|sensor_update + group: "<name>"
- kind: api
  assert: host_count          # hostname: "<name>" + equals: <n>  (visible = non-hidden)
- kind: api
  assert: ioa_group_enabled   # group: "<rule group name>"
- kind: api
  assert: detection_contains  # contains: "<token>" + within_min: <n>
- kind: api
  assert: ml_exclusion        # path_contains: "<token>" + group: "<name>"
- kind: api
  assert: quarantined_file    # hostname: "<name>" + name_contains: "<token>"
- kind: api
  assert: host_group          # group: "<name>"  — the group exists (name resolves to an id)
- kind: api
  assert: hosts_matching      # contains: "<token>" + at_least: <n>  (visible hosts, wildcard FQL)
- kind: api
  assert: ioc_exists          # value_contains / type / action, all optional and ANDed
- kind: api
  assert: rfm_state           # target: win|lnx|mac (or hostname:) + expect: no|yes
```

### `rfm_state`, and why RFM cannot be graded from the host

Added 2026-08-12. `target:` names a lab guest in the lab's own terms (`win`/`lnx`), which
resolves to that host's Falcon hostname *and* puts the right terminal button on the scenario
card; `hostname:` is the escape hatch for a host the lab does not own. `expect: yes` inverts it,
which is what an "induce RFM" exercise wants.

It reads the device field `reduced_functionality_mode` (`yes`/`no`/`unknown`); `unknown` grades
`None`, never a fail, because an older sensor that never populated the field is not evidence of
a problem. Results are keyed by **device id, not hostname** — this CID carries a stale duplicate
`FALCON-LAB-WIN`, and keying by name would collapse two AIDs into one verdict and could report
the healthy twin while hiding the one in RFM.

The reason this has to go through the API is that **asking the host is platform-specific, and on
Windows impossible.** Verified on the lab guests, not assumed:

| | Host-side RFM query |
|---|---|
| Linux | `sudo /opt/CrowdStrike/falconctl -g --rfm-state` → `rfm-state=false.` (trailing period) |
| Windows | **none exists.** There is no `falconctl`. `CSSensorSettings.exe --help` lists only proxy settings, grouping tags and `--no-rtr`. `sc query csagent` shows the driver RUNNING *while the host is in RFM*, so it is a false comfort, not an answer |
| macOS | a different `falconctl` at `/Applications/Falcon.app/Contents/Resources/falconctl`; its RFM output is **unverified here** — no Mac guest yet |

The harvested docs corpus is thin on RFM but settles that it is not Linux-only: *"Network
containment is supported on Windows and macOS hosts running the Falcon sensor in RFM."* So the
cloud-side field is the only question that works fleet-wide — and the only one that will cover
the Mac guest the day it joins, with no new code.

`host_group`, `hosts_matching` and `ioc_exists` were added on 2026-08-12 to migrate five
`kind: console` checks off the browser. Each replaced a substring match on a rendered page with
a structured question, and the difference is not cosmetic: `sensor-update-pinning` had been
passing on the mere presence of the string "Falcon Lab" on the sensor-update policies page, and
correctly reports NOT YET now that the check asks whether a policy is *assigned* to that group.

Two implementation notes worth keeping: group/host **names are resolved client-side** (the FQL
`name:'…'` filter returned null for a group that plainly exists), and API responses can carry
`"resources": null`, so every read normalises with `or []`.

## The web panel

`./lab.py web` serves the same functions as a page, so you can run scenarios yourself without
involving an assistant at all — which is the whole economic point of this package. It must run
somewhere that can reach both the hypervisor and `10.77.0.0/24`, and if console checks are to
work it also needs `CDP_PORT` pointing at a browser signed in to Falcon:

```bash
CDP_PORT=9333 ./lab.py web        # then http://127.0.0.1:8901
```

It binds **loopback** by default — the panel has no auth of its own and `/api/term/{host}` is a
shell on the guests, so publishing it is the job of a layer that authenticates (see
`deployment.md`). `--host 0.0.0.0` is there for the deliberate case and says what it costs.

In the real deployment the panel is not run this way at all; it is a systemd service in the
controller LXC. **`deployment.md` is the file to read** before trying to find or update a
running panel.

Scenarios are grouped by CCFA domain, each card showing its objective, verification kinds and
prerequisites. Each card also carries a **terminal** button for the guest that exercise uses, so
a shell is one click from the instructions rather than a scroll back to the Hosts section. Which
guest that is comes from the scenario itself — its `target`, plus any guest named by an
individual check, so a `target: console` exercise that verifies something on a guest (the IOC one
runs a `Test-Path` on `win`; `rfm-and-inactive-hosts` reads sensor state from `lnx`) still offers
the right shell. Console-only exercises get no button, and an unreachable guest disables it with
the reason rather than opening a window that only prints an SSH failure. Grading renders every check individually rather than a single verdict, so a
mixed result is legible at a glance:

```
  FAIL   Falcon Lab group exists     not found in the console's response: 'Falcon Lab'
  --     understanding               self-checked (not verified automatically)
         ☐ Both lab hosts are members of the group
  NOT YET
```

Host cards offer revert buttons per **baseline** (`bare`, `sensor`) rather than per raw
snapshot name — the page speaks the same vocabulary the scenarios do, instead of guessing
which snapshot means what from its name.

> **No authentication, deliberately.** It can revert VMs and execute code inside guests. Put it
> somewhere only you can reach and do not expose it beyond your own network.

## Sensor installers

Download them from the Falcon console (Host setup and management → Sensor downloads) into
`~/falcon-installers` — local to wherever the CLI runs, because that is where the lab SSH key
already is. Staging them on the hypervisor would mean copying a private key onto it for no
benefit. The directory is gitignored, along with `*.deb`, `*.rpm` and `WindowsSensor*.exe`;
these are licensed binaries and must never be committed.

Retrieval can also be automated against a signed-in console session — see
[Fetching installers automatically](#fetching-installers-automatically) below.

```
./lab.py sensor installers      # what is staged
./lab.py sensor verify          # state of both guests
./lab.py -v sensor verify win   # with cid/aid/rfm detail
```

### Fetching installers automatically

With a Chrome signed in to the console and reachable over the tunnel, the installers can be
pulled without leaving the terminal. The older-versions page is the one worth using:

```
/sensor-downloads/older-versions?os=Windows&osVersion=
/sensor-downloads/older-versions?os=Ubuntu&osVersion=
```

Each row carries the version, the platform, a **SHA256**, and an icon button whose only label
is `aria-label="Download"` — there is no link to follow, so the button must be clicked. Set
`Browser.setDownloadBehavior` to a path on the *browser's* machine first, then copy the file
back. Always verify against the SHA256 the console lists; the page gives it to you, so there
is no reason to trust the transfer blindly.

Deliberately **not** wired into `lab.py`. These are licensed binaries tied to a subscription,
and a repo other people deploy should not automate retrieving them by default. The manual
download stays the documented path; this is a convenience for a tenant you own.

> A note on choosing a version: N-2 is a better lab default than the latest. The sensor
> update policy exercises (objective 5.2) only demonstrate anything if there is a version to
> update *from*.
