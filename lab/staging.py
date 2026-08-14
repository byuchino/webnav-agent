"""Staging prerequisites that are NOT the lesson.

The rule this module exists under: **the lab must never perform the action an objective asks
the learner to perform.** Creating an IOC is the whole point of `ioc-blocklist-hash`, so that
scenario stages nothing. But `quarantined-files` is about MANAGING a quarantined file — the
blocklist entry that produces one is scaffolding, and making the learner hand-build it every run
adds clicks without adding understanding.

The other rule: **anything the lab creates, the lab must not count as evidence.** A stager may
produce the trigger; the graded artifact has to be downstream of it. Here the stager makes an IOC
and the check reads the QUARANTINE store, so a broken stager fails the exercise instead of
passing it.
"""
import re
import uuid

from . import core, falcon

# A benign 64-bit console app, compiled in place. Not a downloaded binary and not a modified
# system file: `Add-Type` uses the C# compiler that ships with Windows PowerShell, so the guest
# needs no toolchain and nothing arrives over the network.
#
# EICAR cannot do this job. It is a 16-bit DOS binary, x64 Windows refuses to load it
# ("not a valid application for this OS platform"), and quarantine is an EXECUTION-time action --
# so EICAR can never reach the code path that quarantines. Verified 2026-08-14; see
# docs/lab-cli.md.
_BUILD = (
    "New-Item -ItemType Directory -Force -Path C:\\lab | Out-Null; "
    "$src = 'public class LabTest {{ public static void Main() {{ "
    "System.Console.WriteLine(\"falcon-lab {marker}\"); }} }}'; "
    "Add-Type -TypeDefinition $src -OutputAssembly C:\\lab\\{name} "
    "-OutputType ConsoleApplication -ErrorAction Stop; "
    "Write-Output ('SHA256=' + (Get-FileHash C:\\lab\\{name} -Algorithm SHA256).Hash)"
)


def quarantine_bait(host="win", group="Falcon Lab", name="labtest.exe"):
    """Build a unique benign executable on `host` and blocklist its hash for `group`.

    Returns {ok, reason, sha256}. Needs IOC Management **write**; without it the IOC create
    reports that plainly rather than failing somewhere confusing later.

    The binary is rebuilt with a fresh marker every run, so its hash is new every time. That is
    deliberate: a stale IOC from a previous run would block the new binary before the learner
    did anything, and the exercise would appear to work while testing nothing.
    """
    # Idempotent: staging means "make the world look like this", not "add another one". Without
    # this, every run of the exercise leaves another IOC blocking the hash of a binary that no
    # longer exists -- exactly the CID clutter that write access was meant to remove. Safe
    # because ioc_clean() can only ever delete what the lab tagged.
    prior = falcon.ioc_clean()
    removed = prior.get("removed") or 0

    marker = uuid.uuid4().hex[:12]
    cmd = _BUILD.format(marker=marker, name=name)
    rc, out, err = core.guest(host, cmd, timeout=240)
    m = re.search(r"SHA256=([0-9A-Fa-f]{64})", out or "")
    if not m:
        return {"ok": False, "sha256": None,
                "reason": f"could not build {name} on {host}: {(err or out or '').strip()[:160]}"}
    sha = m.group(1)
    r = falcon.ioc_create(sha, ioc_type="sha256", action="prevent", host_groups=[group],
                          description=f"{name} — quarantine trigger staged by the lab")
    if r.get("ok") is not True:
        return {"ok": r.get("ok"), "sha256": sha,
                "reason": f"built {name} ({sha[:12]}...) but the IOC was not created: "
                          f"{r.get('reason')}"}
    tidied = f"cleared {removed} stale lab IOC(s); " if removed else ""
    return {"ok": True, "sha256": sha,
            "reason": f"{tidied}built {name} ({sha[:12]}...) and blocklisted it for {group!r}; "
                      f"allow a few minutes for the sensor to receive the IOC"}


STAGERS = {"quarantine_bait": quarantine_bait}


def run(kind, s):
    """Dispatch a scenario `stage:` step. `s` is the scenario, for its target host."""
    fn = STAGERS.get(kind)
    if not fn:
        return {"ok": None, "reason": f"unknown stage {kind!r}; known: {sorted(STAGERS)}"}
    host = s.get("target") if s.get("target") != "console" else "win"
    return fn(host=host)
