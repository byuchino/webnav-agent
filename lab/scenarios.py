"""Scenario definitions and the runner.

Scenarios are YAML so adding one needs no code. Two modes:

- **auto** -- the runner performs the steps. Used for "make something happen on an endpoint
  that already has a sensor, then go find it in the console."
- **manual** -- the runner reverts to the right baseline, prints what to do, and stops. The
  operator does the work themselves and then asks to be graded. This exists because some
  exercises ARE the doing: installing a sensor is on the CCFA syllabus, and a lab that
  installs it for you teaches nothing.

Every scenario declares the baseline it needs, so an exercise cannot silently start from the
wrong state -- a sensor-install lesson that began on a host which already had one would be
quietly useless.
"""
import pathlib

import yaml

from . import config, core, sensor

REQUIRED = ("id", "name", "target", "baseline", "mode")


def load_all():
    out = {}
    for p in sorted(pathlib.Path(config.SCENARIO_DIR).glob("*.yaml")):
        d = yaml.safe_load(p.read_text()) or {}
        missing = [k for k in REQUIRED if k not in d]
        if missing:
            raise ValueError(f"{p.name}: missing {', '.join(missing)}")
        if d["baseline"] not in config.BASELINES:
            raise ValueError(f"{p.name}: baseline must be one of {config.BASELINES}")
        if d["target"] not in config.HOSTS:
            raise ValueError(f"{p.name}: unknown target {d['target']!r}")
        d["_file"] = p.name
        out[d["id"]] = d
    return out


def get(sid):
    all_ = load_all()
    if sid not in all_:
        raise KeyError(f"unknown scenario {sid!r}; known: {', '.join(sorted(all_))}")
    return all_[sid]


def prepare(sid, skip_revert=False):
    """Put the target into the scenario's declared baseline."""
    s = get(sid)
    if skip_revert:
        return {"scenario": sid, "reverted": None}
    r = core.revert(s["target"], s["baseline"])
    return {"scenario": sid, "reverted": r}


def run(sid, skip_revert=False):
    """Execute an auto scenario. Manual scenarios return their instructions instead."""
    s = get(sid)
    result = {"scenario": sid, "name": s["name"], "target": s["target"],
              "mode": s["mode"], "steps": [], "expect": s.get("expect", {})}

    prep = prepare(sid, skip_revert)
    result["prepared"] = prep["reverted"]

    if s["mode"] == "manual":
        result["instructions"] = s.get("instructions", "").strip()
        result["note"] = "manual scenario: perform the steps yourself, then run `lab grade`"
        return result

    for i, step in enumerate(s.get("steps", []), start=1):
        name = step.get("name", f"step {i}")
        cmd = step.get("shell", "").strip()
        if not cmd:
            result["steps"].append({"n": i, "name": name, "ok": None, "out": "no command"})
            continue
        rc, out, err = core.guest(s["target"], cmd, timeout=step.get("timeout", 300))
        ok = (rc == 0)
        if "expect_contains" in step:
            ok = ok and (step["expect_contains"] in (out or ""))
        result["steps"].append({"n": i, "name": name, "ok": ok,
                                "out": (out or err or "").strip()[:400]})
        if not ok and step.get("required", True):
            result["failed_at"] = i
            break
    return result


def grade(sid):
    """Check whether the operator achieved the scenario's goal. Reports what is true, not
    what was attempted -- the same principle as the agent's verification layer."""
    s = get(sid)
    g = s.get("grade") or {}
    kind = g.get("kind", "shell")

    if kind == "sensor":
        r = sensor.verify(s["target"])
        passed = bool(r.get("installed") and r.get("running") and r.get("aid"))
        rfm = r.get("rfm")
        if passed and rfm and str(rfm).lower() not in ("false", "0", "none"):
            passed = False
        return {"scenario": sid, "passed": passed, "detail": r,
                "verdict": r.get("verdict", ""),
                "hint": g.get("hint", "") if not passed else ""}

    cmd = g.get("shell", "").strip()
    if not cmd:
        return {"scenario": sid, "passed": None,
                "verdict": "no automated grading for this scenario -- check the console"}
    rc, out, err = core.guest(s["target"], cmd, timeout=g.get("timeout", 120))
    passed = (rc == 0)
    if "expect_contains" in g:
        passed = passed and (g["expect_contains"] in (out or ""))
    return {"scenario": sid, "passed": passed, "verdict": (out or err).strip()[:300],
            "hint": g.get("hint", "") if not passed else ""}
