"""Talking to the hypervisor and the guests.

Every function here returns structured data and prints nothing. Output formatting lives in
the CLI, because the same calls back the web panel — and because terse, predictable output
is the whole point of this package: a scenario run should cost a few lines, not a
conversation.
"""
import shlex
import socket
import subprocess
import time

from . import config

SSH_BASE = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR"]


class LabError(RuntimeError):
    pass


def _run(cmd, timeout=120):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


# --- hypervisor -----------------------------------------------------------------------
def pve(command, timeout=180):
    """Run a command on the Proxmox host."""
    rc, out, err = _run(SSH_BASE + [config.PVE, command], timeout)
    if rc != 0:
        raise LabError(f"pve: {command[:60]}: {err or out}")
    return out


def _ctl(h):
    return "pct" if h["kind"] == "lxc" else "qm"


def vm_status(name):
    h = config.host(name)
    try:
        out = pve(f"{_ctl(h)} status {h['vmid']}")
        return out.split()[-1] if out else "unknown"
    except LabError:
        return "error"


def vm_start(name):
    h = config.host(name)
    if vm_status(name) == "running":
        return "already running"
    pve(f"{_ctl(h)} start {h['vmid']}")
    return "started"


def vm_stop(name):
    h = config.host(name)
    if vm_status(name) == "stopped":
        return "already stopped"
    pve(f"{_ctl(h)} stop {h['vmid']}")
    return "stopped"


def snapshots(name):
    h = config.host(name)
    try:
        out = pve(f"{_ctl(h)} listsnapshot {h['vmid']}")
    except LabError:
        return []
    names = []
    for line in out.splitlines():
        line = line.strip().lstrip("`->").strip()
        if not line or line.startswith("current"):
            continue
        names.append(line.split()[0])
    return names


def snapshot_create(name, snap, description=""):
    h = config.host(name)
    d = f" --description {shlex.quote(description)}" if description else ""
    pve(f"{_ctl(h)} snapshot {h['vmid']} {shlex.quote(snap)}{d}", timeout=600)
    return snap


def revert(name, baseline="bare", wait=True):
    """Roll a guest back to a baseline. Seconds, versus a 20-minute reinstall -- which is
    most of why the lab is worth having: every exercise starts from an identical state."""
    h = config.host(name)
    snap = h["snapshots"].get(baseline)
    if not snap:
        raise LabError(f"{name}: no '{baseline}' baseline defined")
    if snap not in snapshots(name):
        have = ", ".join(snapshots(name)) or "none"
        extra = ""
        if baseline == "sensor":
            # The usual cause: the sensor baseline has never been built. Say how.
            extra = (f"\n  Build it once:  ./lab.py sensor install {name} --ccid <CID>"
                     f"\n                  ./lab.py snapshot {name} {snap} -d 'sensor registered'")
        raise LabError(f"{name}: no {snap!r} snapshot yet (have: {have}){extra}")
    pve(f"{_ctl(h)} rollback {h['vmid']} {shlex.quote(snap)}", timeout=600)
    pve(f"{_ctl(h)} start {h['vmid']} || true")
    if not wait:
        return {"snapshot": snap, "ready": None}
    ok, secs = wait_ready(name)
    return {"snapshot": snap, "ready": ok, "seconds": secs}


# --- guests ---------------------------------------------------------------------------
def reachable(name, timeout=4):
    """TCP probe. Windows Firewall drops ICMP by default, so ping is not a liveness test."""
    h = config.host(name)
    port = h.get("probe_port")
    if not port:
        return vm_status(name) == "running"
    try:
        with socket.create_connection((h["ip"], port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_ready(name, limit=300):
    t0 = time.time()
    while time.time() - t0 < limit:
        if reachable(name):
            return True, round(time.time() - t0)
        time.sleep(5)
    return False, round(time.time() - t0)


def guest(name, command, timeout=180, check=False):
    """Run a command inside a guest over SSH. Returns (rc, stdout, stderr).

    Windows guests have PowerShell as their SSH shell, so `command` is PowerShell there and
    sh there. Callers that need both provide both.
    """
    h = config.host(name)
    cmd = SSH_BASE + ["-i", config.LAB_KEY, f"{h['user']}@{h['ip']}", command]
    rc, out, err = _run(cmd, timeout)
    if check and rc != 0:
        raise LabError(f"{name}: {err or out}")
    return rc, out, err


def push(name, local_path, remote_path, timeout=600):
    h = config.host(name)
    cmd = ["scp", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
           "-o", "LogLevel=ERROR", "-i", config.LAB_KEY,
           local_path, f"{h['user']}@{h['ip']}:{remote_path}"]
    rc, out, err = _run(cmd, timeout)
    if rc != 0:
        raise LabError(f"{name}: push failed: {err or out}")
    return remote_path


def status(name):
    h = config.host(name)
    st = vm_status(name)
    return {
        "host": name,
        "name": h["name"],
        "ip": h["ip"],
        "vm": st,
        "reachable": reachable(name) if st == "running" else False,
        "snapshots": snapshots(name),
    }
