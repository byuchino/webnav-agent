"""Scenario definitions and the runner.

Scenarios are YAML so adding one needs no code.

## Three targets, because most of CCFA never touches an endpoint

    target: win | lnx    an exercise performed on a guest
    target: console      pure console work -- roles, policies, IOCs, workflows

Console scenarios have no guest and no baseline to revert to. Treating them as endpoint
scenarios with an unused host would have been a lie the schema told about itself.

## Three modes

    auto      the runner performs the steps
    manual    the runner prepares and stops; you do the work, then ask to be graded
    guided    the runner does the setup and you do the interesting part

`manual` exists because some exercises ARE the doing. Installing a sensor is on the syllabus;
a lab that installs it for you teaches nothing.

## Verification is a list, not a value

    verify:
      - kind: console     navigate, capture the API response, assert on it
      - kind: endpoint    run a command on the guest and assert on the output
      - kind: sensor      the full sensor health chain
      - kind: attest      a checklist, honestly marked as unverified

A scenario can require several. That combination is the point: "configured" and "functional"
are different claims, and the gap between them is where policy precedence, group assignment
and propagation delay actually live. A console check alone tells you the setting was saved;
an endpoint check alone tells you the behaviour changed without saying why.

A check that cannot run (no console session, guest unreachable) returns `None`, and `None` is
never a pass. "I could not look" and "I looked and it was wrong" are different answers.
"""
import pathlib

import yaml

from . import config, core, falcon, sensor

REQUIRED = ("id", "name", "target", "mode")
VALID_TARGETS = set(config.HOSTS) | {"console"}
VALID_MODES = {"auto", "manual", "guided"}
VALID_KINDS = {"console", "endpoint", "sensor", "attest"}


def load_all():
    out = {}
    for p in sorted(pathlib.Path(config.SCENARIO_DIR).rglob("*.yaml")):
        d = yaml.safe_load(p.read_text()) or {}
        missing = [k for k in REQUIRED if k not in d]
        if missing:
            raise ValueError(f"{p.name}: missing {', '.join(missing)}")
        if d["target"] not in VALID_TARGETS:
            raise ValueError(f"{p.name}: target must be one of {sorted(VALID_TARGETS)}")
        if d["mode"] not in VALID_MODES:
            raise ValueError(f"{p.name}: mode must be one of {sorted(VALID_MODES)}")
        # Console scenarios have no guest, so no baseline. Endpoint ones must declare theirs.
        if d["target"] == "console":
            d.setdefault("baseline", "none")
        else:
            if "baseline" not in d:
                raise ValueError(f"{p.name}: endpoint scenarios must declare a baseline")
            if d["baseline"] not in config.BASELINES:
                raise ValueError(f"{p.name}: baseline must be one of {config.BASELINES}")
        for v in d.get("verify") or []:
            if v.get("kind") not in VALID_KINDS:
                raise ValueError(f"{p.name}: verify kind must be one of {sorted(VALID_KINDS)}")
        d["_file"] = str(p.relative_to(config.SCENARIO_DIR))
        d.setdefault("domain", 0)
        d.setdefault("difficulty", 1)
        out[d["id"]] = d
    return out


def get(sid):
    all_ = load_all()
    if sid not in all_:
        raise KeyError(f"unknown scenario {sid!r}; known: {', '.join(sorted(all_))}")
    return all_[sid]


def unmet_prerequisites(sid, all_=None):
    """Prerequisites are advisory -- reported, never enforced. Someone re-running one
    exercise should not be made to redo the three before it."""
    all_ = all_ or load_all()
    s = all_.get(sid, {})
    return [r for r in (s.get("requires") or []) if r not in all_]


def prepare(sid, skip_revert=False):
    s = get(sid)
    if s["target"] == "console" or s.get("baseline") == "none" or skip_revert:
        return None
    return core.revert(s["target"], s["baseline"])


def run(sid, skip_revert=False):
    s = get(sid)
    result = {"scenario": sid, "name": s["name"], "target": s["target"],
              "mode": s["mode"], "domain": s.get("domain"), "steps": [],
              "expect": s.get("expect", {})}

    result["prepared"] = prepare(sid, skip_revert)

    if s["mode"] in ("manual", "guided"):
        result["instructions"] = (s.get("instructions") or "").strip()

    # `guided` runs its setup steps and then hands over; `manual` runs nothing.
    steps = s.get("setup") if s["mode"] == "guided" else (
        s.get("steps") if s["mode"] == "auto" else [])
    for i, step in enumerate(steps or [], start=1):
        name = step.get("name", f"step {i}")
        cmd = (step.get("shell") or "").strip()
        if not cmd:
            continue
        host = step.get("target") or (s["target"] if s["target"] != "console" else None)
        if not host:
            result["steps"].append({"n": i, "name": name, "ok": None,
                                    "out": "console scenario: no guest to run on"})
            continue
        rc, out, err = core.guest(host, cmd, timeout=step.get("timeout", 300))
        ok = (rc == 0)
        if "expect_contains" in step:
            ok = ok and (step["expect_contains"] in (out or ""))
        result["steps"].append({"n": i, "name": name, "ok": ok,
                                "out": (out or err or "").strip()[:400]})
        if not ok and step.get("required", True):
            result["failed_at"] = i
            break
    return result


# --- grading --------------------------------------------------------------------------
def _check_endpoint(s, v):
    host = v.get("target") or s["target"]
    if host == "console":
        return {"ok": None, "reason": "endpoint check on a console scenario needs a target"}
    if not core.reachable(host):
        return {"ok": None, "reason": f"{host} is not reachable"}
    rc, out, err = core.guest(host, (v.get("shell") or "").strip(),
                              timeout=v.get("timeout", 120))
    ok = (rc == 0)
    if "expect_contains" in v:
        ok = ok and (v["expect_contains"] in (out or ""))
    if "expect_absent" in v:
        ok = ok and (v["expect_absent"] not in (out or ""))
    return {"ok": ok, "reason": (out or err).strip()[:200] or f"exit {rc}"}


def _check_sensor(s, v):
    host = v.get("target") or s["target"]
    r = sensor.verify(host)
    if not r.get("reachable"):
        return {"ok": None, "reason": r.get("verdict", "unreachable")}
    ok = bool(r.get("installed") and r.get("running") and r.get("aid"))
    rfm = r.get("rfm")
    if ok and rfm and str(rfm).lower() not in ("false", "0", "none"):
        ok = False
    return {"ok": ok, "reason": r.get("verdict", "")}


def _check_console(s, v):
    return falcon.check(v.get("path", "/"), v.get("api_match", "/api/"),
                        v.get("expect_contains"), v.get("expect_absent"),
                        settle=v.get("settle", 14))


def _check_attest(s, v):
    return {"ok": None, "reason": "self-checked (not verified automatically)",
            "items": v.get("items") or []}


_CHECKERS = {"console": _check_console, "endpoint": _check_endpoint,
             "sensor": _check_sensor, "attest": _check_attest}


def grade(sid):
    """Run every declared check. Passes only if at least one check ran and none failed.

    A scenario whose checks all returned None is NOT a pass -- it is unverified, and says so.
    """
    s = get(sid)
    checks = s.get("verify") or []
    if not checks:
        return {"scenario": sid, "passed": None, "checks": [],
                "verdict": "no verification defined for this scenario"}

    results = []
    for v in checks:
        fn = _CHECKERS[v["kind"]]
        try:
            r = fn(s, v)
        except Exception as e:  # noqa: BLE001
            r = {"ok": None, "reason": f"check errored: {str(e)[:120]}"}
        r["kind"] = v["kind"]
        r["label"] = v.get("label", v["kind"])
        results.append(r)

    ran = [r for r in results if r["ok"] is not None]
    failed = [r for r in ran if r["ok"] is False]
    passed = None if not ran else (not failed)

    if passed is True:
        verdict = "; ".join(f"{r['label']}: ok" for r in ran)
    elif passed is False:
        verdict = "; ".join(f"{r['label']}: {r['reason']}" for r in failed)
    else:
        verdict = "; ".join(f"{r['label']}: {r['reason']}" for r in results) or "nothing ran"

    return {"scenario": sid, "passed": passed, "checks": results, "verdict": verdict,
            "hint": (s.get("hint") or "").strip() if passed is not True else ""}
