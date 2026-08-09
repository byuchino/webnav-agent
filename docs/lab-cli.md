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

Drop a YAML file in `scenarios/`. No code.

```yaml
id: my-scenario
name: Human readable name
target: win            # win | lnx
baseline: sensor       # bare | sensor
mode: auto             # auto = runner executes; manual = you do it
summary: one line
teaches: |
  what the operator should take away
steps:                 # auto only
  - name: do the thing
    shell: |
      Write-Output 'powershell on win, sh on lnx'
    expect_contains: optional
expect:
  console: where to look in Falcon
  detection: what should fire
grade:
  kind: sensor         # or omit for a shell check
  shell: |
    optional command; rc 0 plus expect_contains is a pass
  hint: shown only on failure
```

## The web panel

`./lab.py web` serves the same functions as a page, so you can run scenarios yourself without
involving an assistant at all. It must run somewhere that can reach both the hypervisor and
`10.77.0.0/24` — this workstation qualifies; the always-on `docker-vm` would be a better
long-term home.

> **No authentication, deliberately.** It can revert VMs and execute code inside guests. Put it
> somewhere only you can reach and do not expose it beyond your own network.

## Sensor installers

Download them from the Falcon console (Host setup and management → Sensor downloads) into
`~/falcon-installers`. This can also be automated against a signed-in console session — see
"Fetching installers" below — local to wherever the CLI runs, because that is where the lab SSH key
already is. Staging them on the hypervisor would mean copying a private key onto it for no
benefit. The directory is gitignored, along with `*.deb`, `*.rpm` and `WindowsSensor*.exe`;
these are licensed binaries and must never be committed.

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
