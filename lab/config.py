"""Lab inventory. One place to change when the lab moves.

Everything else in this package reads from here, so a rebuild on different hardware means
editing this file and nothing else.
"""
import os
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
SCENARIO_DIR = REPO / "scenarios"

# Proxmox host, reached by SSH alias from ~/.ssh/config.
PVE = os.environ.get("LAB_PVE", "proxmox-1")

# The key that reaches the guests. Deliberately its own key, not reused from anything else.
LAB_KEY = os.environ.get("LAB_KEY", str(pathlib.Path.home() / ".ssh" / "falcon_lab_ed25519"))

LAB_NET = "10.77.0.0/24"
LAB_GW = "10.77.0.1"

# Two baselines per guest, because installing the sensor is itself an exercise (it is on the
# CCFA syllabus). `bare` is the starting point for that lesson; `sensor` is where every
# detection scenario starts. A scenario declares which one it needs and the runner reverts
# to it, so an exercise can never accidentally begin from the wrong state.
BASELINES = ("bare", "sensor")

HOSTS = {
    "win": {
        "vmid": 900,
        "kind": "qemu",
        "os": "windows",
        "ip": "10.77.0.10",
        "user": "labadmin",
        "name": "falcon-lab-win",
        "snapshots": {"bare": "clean-preSensor", "sensor": "clean-withSensor"},
        # Windows has no ICMP by default -- never test liveness with ping.
        "probe_port": 22,
    },
    "lnx": {
        "vmid": 901,
        "kind": "qemu",
        "os": "linux",
        "ip": "10.77.0.11",
        "user": "labadmin",
        "name": "falcon-lab-lnx",
        "snapshots": {"bare": "clean-cloudinit", "sensor": "clean-withSensor"},
        "probe_port": 22,
    },
    "dhcp": {
        "vmid": 902,
        "kind": "lxc",
        "os": "linux",
        "ip": "10.77.0.2",
        "user": "root",
        "name": "falcon-lab-dhcp",
        "snapshots": {},
        "probe_port": None,
    },
}

GUESTS = [k for k, v in HOSTS.items() if v["snapshots"]]

# Where you drop the sensor packages downloaded from the Falcon console. Kept OUTSIDE the
# repo so installers are never committed, and local to wherever the CLI runs, because that
# is where the lab key already is -- staging via the hypervisor would mean copying a private
# key onto it for no benefit.
INSTALLER_DIR = os.environ.get(
    "LAB_INSTALLERS", str(pathlib.Path.home() / "falcon-installers"))
WIN_STAGE = r"C:\lab"
LNX_STAGE = "/opt/lab"


def host(name):
    if name not in HOSTS:
        raise KeyError(f"unknown host {name!r}; known: {', '.join(HOSTS)}")
    return HOSTS[name]
