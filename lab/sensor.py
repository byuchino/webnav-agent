"""Falcon sensor install, verification and removal.

`install` and `verify` are deliberately separate. Installing the sensor is itself an exercise
on the CCFA syllabus, so for that lesson the operator does it by hand on a `bare` guest and
`verify` grades the result without doing any of it for them. `install` exists to build the
`sensor` baseline quickly for every other scenario.

`verify` reports what is true, never what was intended: service state, CID, AID, and on Linux
the RFM state -- Reduced Functionality Mode, which is where a sensor lands when it does not
have a matching kernel module. A sensor in RFM looks installed and running while collecting
almost nothing, so "the service is up" is not the same as "the sensor is working." That
distinction is exactly what the exercise should teach.
"""
import pathlib
import re

from . import config, core

# --- verification ---------------------------------------------------------------------
_WIN_AID_KEY = (r"HKLM:\SYSTEM\CrowdStrike\{9b03c1d9-3138-44ed-9fae-d9f4c034b88d}"
                r"\{16e0423f-7058-48c9-a204-725362b67639}\Default")


def _verify_windows(name):
    rc, out, _ = core.guest(name, (
        "$s = Get-Service CSFalconService -ErrorAction SilentlyContinue; "
        "$svc = if ($s) { $s.Status } else { 'absent' }; "
        f"$ag = (Get-ItemProperty -Path '{_WIN_AID_KEY}' -Name AG -ErrorAction SilentlyContinue).AG; "
        "$aid = if ($ag) { ($ag | ForEach-Object { $_.ToString('x2') }) -join '' } else { '' }; "
        f"$cu = (Get-ItemProperty -Path '{_WIN_AID_KEY}' -Name CU -ErrorAction SilentlyContinue).CU; "
        "$cid = if ($cu) { ($cu | ForEach-Object { $_.ToString('x2') }) -join '' } else { '' }; "
        "$ver = (Get-ItemProperty 'C:\\Program Files\\CrowdStrike\\CSFalconService.exe' "
        "  -ErrorAction SilentlyContinue).VersionInfo.ProductVersion; "
        "\"svc=$svc`naid=$aid`ncid=$cid`nver=$ver\""), timeout=90)
    d = dict(re.findall(r"^(\w+)=(.*)$", out, re.M)) if rc == 0 else {}
    svc = d.get("svc", "unknown")
    return {
        "installed": svc != "absent" and svc != "unknown",
        "service": svc,
        "running": svc.lower() == "running",
        "aid": d.get("aid") or None,
        "cid": d.get("cid") or None,
        "version": d.get("ver") or None,
        "rfm": None,          # not applicable on Windows
    }


def _verify_linux(name):
    # `systemctl is-active` prints "inactive" for a unit that does not exist at all, so it
    # cannot distinguish "not installed" from "installed but stopped". Presence of falconctl
    # is the honest test.
    rc, out, _ = core.guest(name, (
        "printf 'pkg=%s\\n' \"$([ -x /opt/CrowdStrike/falconctl ] && echo yes || echo no)\"; "
        "printf 'svc=%s\\n' \"$(systemctl is-active falcon-sensor 2>/dev/null || echo inactive)\"; "
        "if [ -x /opt/CrowdStrike/falconctl ]; then "
        "  printf 'cid=%s\\n' \"$(sudo -n /opt/CrowdStrike/falconctl -g --cid 2>/dev/null | tr -d '\\n')\"; "
        "  printf 'aid=%s\\n' \"$(sudo -n /opt/CrowdStrike/falconctl -g --aid 2>/dev/null | tr -d '\\n')\"; "
        "  printf 'rfm=%s\\n' \"$(sudo -n /opt/CrowdStrike/falconctl -g --rfm-state 2>/dev/null | tr -d '\\n')\"; "
        "  printf 'ver=%s\\n' \"$(dpkg-query -W -f='${Version}' falcon-sensor 2>/dev/null)\"; "
        "else printf 'cid=\\naid=\\nrfm=\\nver=\\n'; fi"), timeout=90)
    d = dict(re.findall(r"^(\w+)=(.*)$", out, re.M)) if rc == 0 else {}

    def _val(raw):
        # falconctl prints things like: cid="abc123...", rfm-state=false
        if not raw:
            return None
        m = re.search(r'[=:]\s*"?([^",]+)"?', raw)
        return (m.group(1) if m else raw).strip() or None

    svc = d.get("svc", "unknown")
    rfm = _val(d.get("rfm"))
    return {
        "installed": d.get("pkg") == "yes",
        "service": svc,
        "running": svc == "active",
        "aid": _val(d.get("aid")),
        "cid": _val(d.get("cid")),
        "version": d.get("ver") or None,
        "rfm": rfm,
    }


def verify(name):
    """What is actually true on this host. Never raises -- an unreachable or bare guest is a
    normal outcome for the install exercise, not an error."""
    h = config.host(name)
    if not core.reachable(name):
        return {"host": name, "reachable": False, "installed": False,
                "verdict": "guest not reachable"}
    try:
        r = _verify_windows(name) if h["os"] == "windows" else _verify_linux(name)
    except Exception as e:  # noqa: BLE001
        return {"host": name, "reachable": True, "installed": False,
                "verdict": f"check failed: {str(e)[:80]}"}
    r.update({"host": name, "reachable": True})
    r["verdict"] = _verdict(r)
    return r


def _verdict(r):
    """One line an operator can act on."""
    if not r["installed"]:
        return "sensor NOT installed"
    if not r["running"]:
        return f"installed but service is {r['service']}"
    if not r.get("aid"):
        return "running, but no AID yet -- it has not registered with the cloud"
    if r.get("rfm") and str(r["rfm"]).lower() not in ("false", "0", "none"):
        return f"running in REDUCED FUNCTIONALITY MODE (rfm={r['rfm']}) -- little telemetry"
    return "sensor healthy and registered"


# --- installation ---------------------------------------------------------------------
def installers():
    """Installer packages you have downloaded from the Falcon console."""
    d = pathlib.Path(config.INSTALLER_DIR)
    if not d.is_dir():
        return []
    return sorted(f.name for f in d.iterdir() if f.is_file())


def install(name, ccid, installer=None):
    """Automated install -- used to BUILD the `sensor` baseline, not for the exercise."""
    h = config.host(name)
    have = installers()
    if not have:
        raise core.LabError(
            f"no installers staged. Download them from the Falcon console "
            f"(Host setup and management > Sensor downloads) into {config.INSTALLER_DIR}.")
    if installer is None:
        want = ".exe" if h["os"] == "windows" else ".deb"
        cand = [f for f in have if f.lower().endswith(want)]
        if not cand:
            raise core.LabError(f"no {want} installer among: {', '.join(have)}")
        installer = cand[0]

    local = str(pathlib.Path(config.INSTALLER_DIR) / installer)
    if h["os"] == "windows":
        remote = f"{config.WIN_STAGE}\\{installer}"
        core.guest(name, f"New-Item -ItemType Directory -Force -Path '{config.WIN_STAGE}' | Out-Null")
        core.push(name, local, f"{config.WIN_STAGE}/{installer}")
        rc, out, err = core.guest(
            name, f"& '{remote}' /install /quiet /norestart CID={ccid}; $LASTEXITCODE", timeout=900)
    else:
        remote = f"{config.LNX_STAGE}/{installer}"
        core.guest(name, f"sudo -n mkdir -p {config.LNX_STAGE} && sudo -n chown $USER {config.LNX_STAGE}")
        core.push(name, local, remote)
        rc, out, err = core.guest(name, (
            f"sudo -n dpkg -i {remote} >/dev/null 2>&1; "
            f"sudo -n /opt/CrowdStrike/falconctl -s --cid={ccid} && "
            f"sudo -n systemctl start falcon-sensor && echo started"), timeout=600)
    return {"installer": installer, "rc": rc, "output": (out or err)[:300]}


def remove(name, maintenance_token=None):
    h = config.host(name)
    if h["os"] == "windows":
        tok = f" MAINTENANCE_TOKEN={maintenance_token}" if maintenance_token else ""
        cmd = (f"Get-ChildItem '{config.WIN_STAGE}' -Filter *.exe | "
               f"Select-Object -First 1 | ForEach-Object {{ & $_.FullName /uninstall /quiet{tok} }}")
        rc, out, err = core.guest(name, cmd, timeout=900)
    else:
        rc, out, err = core.guest(
            name, "sudo -n systemctl stop falcon-sensor; sudo -n apt-get -qq remove -y falcon-sensor",
            timeout=600)
    return {"rc": rc, "output": (out or err)[:300]}
