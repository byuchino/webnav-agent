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

## The web panel

`./lab.py web` serves the same functions as a page, so you can run scenarios yourself without
involving an assistant at all — which is the whole economic point of this package. It must run
somewhere that can reach both the hypervisor and `10.77.0.0/24`, and if console checks are to
work it also needs `CDP_PORT` pointing at a browser signed in to Falcon:

```bash
CDP_PORT=9333 ./lab.py web        # then http://<this-host>:8901
```

Scenarios are grouped by CCFA domain, each card showing its objective, verification kinds and
prerequisites. Grading renders every check individually rather than a single verdict, so a
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
