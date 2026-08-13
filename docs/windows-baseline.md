# The Windows guest baseline: what has been done to it, and why

`falcon-lab-win` (VM 900) is not a stock Windows install. Four deliberate modifications keep it
usable as a teaching target, and every one of them is a trade against how a real endpoint behaves.
They are recorded here because a snapshot hides its own history: `clean-withSensor` looks like a
clean Windows box and is not.

All were applied 2026-08-13 after a session where the exercise failed four separate ways and three
of them presented identically as "no detection appeared".

## 1. Windows Update is disabled — the OS build is PINNED

**Why.** The newest CrowdStrike sensor available to this CID (`7.40.21306`, build `21306|n`) does
not support Windows 11 26100 at **UBR 9168**. Windows Update walks the guest from the baseline's
UBR 8875 to 9168 on its own, the kernel driver then refuses to load, and the host drops into
**RFM** — where it applies no prevention policy and raises no file detections. Observed twice in
one session, once overnight, with the baseline healthy for only a few hours each time.

**This is not a pinning artifact and could not be fixed by updating the sensor.** The sensor is
already on `n`, the latest build offered (`query_combined_builds` confirms 21306 is newest). It is
vendor certification lagging a Windows update — a real-world condition. Pinning the OS was the
only remaining lever.

**The procedure — all five steps, in order.** Steps 3 and 4 are not optional; skipping them
produced a baseline that booted straight into RFM, and the fault was invisible until it was
reverted to.

```powershell
# 1. Group Policy: no automatic updates
HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU  NoAutoUpdate = 1 (DWord)

# 2. Three services, all set to Start=4 (disabled) in the registry
HKLM\SYSTEM\CurrentControlSet\Services\wuauserv      Start = 4
HKLM\SYSTEM\CurrentControlSet\Services\UsoSvc        Start = 4
HKLM\SYSTEM\CurrentControlSet\Services\WaaSMedicSvc  Start = 4

# 3. Clear anything ALREADY staged
Remove-Item C:\Windows\SoftwareDistribution\Download\* -Recurse -Force

# 4. Reboot, then verify UBR is unchanged        <-- the actual proof
# 5. Only now take the snapshot
```

**All three services matter.** Disabling `wuauserv` alone does not hold: `UsoSvc` (Update
Orchestrator) re-triggers scans, and **`WaaSMedicSvc` (Update Medic) actively repairs disabled
update components** and will undo the change. `WaaSMedicSvc` rejects `Set-Service` with access
denied, which is why all three are set through the registry rather than the service API.

**Steps 3–4 exist because of a real failure (2026-08-13).** Disabling Windows Update stops new
downloads; it does **nothing about updates already downloaded**. Nine packages were sitting in
`SoftwareDistribution\Download`, and **Windows applies staged updates during shutdown** — which is
exactly what `snapshot_create` does before snapshotting. So the build advanced 8875 → 9168 *inside
the snapshot operation*, after the state had been verified, and the resulting `clean-withSensor-
pinned` booted into RFM.

The lesson generalises past Windows Update: **verifying state at a moment in time is not the same
as verifying it survives a shutdown, and a snapshot only ever captures the latter.** Reboot first,
re-verify, then snapshot. And always test a new baseline by reverting to it — an unbootable or
mis-captured snapshot is invisible until something rolls back to it.

**The cost, stated plainly:** this guest no longer receives security updates. That is acceptable
for a snapshot-reverted lab VM that exists to be attacked with EICAR, and unacceptable as a
pattern to copy anywhere else. It also **freezes one half of a genuinely instructive situation** —
"latest sensor + latest Windows = RFM" is exactly what a Falcon admin hits in production, and it
is arguably worth its own scenario rather than only being engineered away.

**To reverse:** set the three `Start` values back (`wuauserv`=3, `UsoSvc`=2, `WaaSMedicSvc`=3) and
delete `NoAutoUpdate`. Expect RFM within hours.

**This is only durable once it is in the snapshot.** Until `clean-withSensor` is re-taken with
these settings, every revert restores an unpinned guest and the decay resumes.

## 2. The clock must be resynced after every revert

A revert restores the clock stored in the snapshot. Observed **63 minutes** behind on one revert
and **7 hours** behind on another. `w32tm` reports `Source: Local CMOS Clock` and
`Last Successful Sync Time: unspecified` — it has never successfully synced.

**Why it matters beyond tidiness:** detections carry timestamps. A guest an hour behind puts its
detections an hour in the past, where a "last hour" filter in Endpoint detections hides them —
producing "no detection appeared" for a detection that fired correctly.

`prevention-detect-vs-block` now resyncs in its `setup:` block. Anything else that reverts `win`
should do the same, or state the skew:

```powershell
w32tm /config /manualpeerlist:"time.windows.com,0x9 pool.ntp.org,0x9" /syncfromflags:manual /update
Restart-Service w32time
w32tm /resync /force
```

A large skew may be rejected by w32time's phase-offset limit; `Set-Date` to a known-good UTC first,
then resync, is the reliable order.

## 3. Defender's state is NOT what a fresh install has — and it survives reverts

After the guest ran under a Phase 2 policy, **Windows Defender is in Passive Mode with real-time
protection off**. It stood down on its own ~45 s after the policy applied, because Phase 2 carries
**Quarantine & Security Center Registration** and Falcon registered as the AV provider.

**This state is sensor-side, not filesystem state, so a snapshot revert does NOT restore it.**
Reverting to `clean-withSensor` yields a guest whose snapshot predates the registration but whose
Defender is still passive.

**Consequence:** `prevention-detect-vs-block`'s Phase 1 beat depends on Defender being *active*
(that is the whole co-residency lesson — two engines, and disk state that cannot attribute an
action). On the current guest that beat will not reproduce.

**RESOLVED 2026-08-13 — it is reversible by policy alone, and the fix is free.** Moving the host
group back to a policy with Security Center Registration **off** makes Falcon unregister and
Defender re-arm itself:

```
10:35:35  host check-in; applied policy switches to the Phase 1 duplicate (pending)
10:38:08  Passive Mode  rtp=False
10:38:44  Normal        rtp=True     <-- re-armed, ~3 min after the policy switch
10:39:27  applied_date recorded by the cloud
```

So `prevention-detect-vs-block` needs **no host-side intervention**: its own step 2, which puts the
group on a Phase 1 policy, restores co-residency as a side effect. Allow ~3 minutes and verify
`AMRunningMode` is `Normal` before running the Phase 1 beat, or the co-residency lesson silently
won't reproduce.

**Note the ordering above:** Defender re-armed at 10:38:44 but the cloud only recorded
`applied_date` at 10:39:27, **43 s later**. The endpoint enforced the policy *before* the cloud
said it had. `applied` is a **trailing** indicator — good for "has it definitely landed", useless
for "has it not landed yet". When the two disagree, the host is right.

Do **not** "fix" this with a Defender exclusion for `C:\lab`. It was considered and rejected: it
suppresses a real, teachable product behaviour to make a check pass, and the phased-rollout design
removed the need entirely. (It is also blocked by the assistant harness — see below.)

## Working on this guest: two mechanical gotchas

**PowerShell over SSH — pipe the script via stdin, and keep every block on ONE line.**

```bash
ssh -i ~/.ssh/falcon_lab_ed25519 labadmin@10.77.0.10 "powershell -NoProfile -Command -" < script.ps1
```

Inline `-Command "..."` mangles `$_` and nested quotes through two layers of shell. But stdin has
its own trap: **`-Command -` evaluates statement by statement, so a `foreach`/`if` block spanning
multiple lines silently does nothing** — no output, no error, exit code 0. This cost a debugging
cycle. Single-line blocks (`foreach ($x in $y) { ... }` all on one line) work; multi-line ones do
not. When a script "returns nothing", suspect this before suspecting the guest.

**Some of this is blocked by the assistant harness.** `Add-MpPreference -ExclusionPath` is
defense-evasion shaped and is refused by the permission classifier, as is base64
`-EncodedCommand` (it reads as obfuscation regardless of intent). The Windows Update registry
changes above were *not* blocked — a documented Group Policy setting reads differently. When
blocked, the assistant should hand over the command rather than route around it; working around it
is precisely what the block exists to prevent.

## What still has to happen for any of this to stick

The pinning and the clock fix are applied to the **running guest only**. Re-take
`clean-withSensor` with a clean shutdown (a live Windows snapshot can capture NTFS mid-write and
produce an unbootable rollback) and keep the current snapshot until the new one is verified
bootable. Until then, every revert undoes section 1.
