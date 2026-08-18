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

from . import config, core, falcon, sensor, staging

REQUIRED = ("id", "name", "target", "mode")
VALID_TARGETS = set(config.HOSTS) | {"console"}
VALID_MODES = {"auto", "manual", "guided"}
VALID_KINDS = {"console", "endpoint", "sensor", "attest", "api"}


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


def prep_steps(s):
    """What pressing "set up" will actually do, derived from the definition rather than
    written by hand -- a hand-written description drifts from the behaviour it describes."""
    out = []
    if s["target"] == "console" or s.get("baseline") in (None, "none"):
        out.append("Nothing is changed on any guest; this is console work.")
    else:
        host, base = s["target"], s["baseline"]
        snap = (config.HOSTS.get(host, {}).get("snapshots") or {}).get(base, base)
        out.append(f"Rolls {host} back to the '{base}' baseline (snapshot {snap}), "
                   f"discarding whatever is on it now.")
        if base == "bare":
            out.append("That baseline has NO sensor — the installed sensor is discarded, and "
                       "reinstalling will register a new agent ID.")
        elif base == "rfm":
            out.append("That baseline has a sensor deliberately in Reduced Functionality Mode: "
                       "it runs and reports in, and collects almost nothing.")
        else:
            out.append("That baseline has a healthy, registered sensor.")
        out.append("Then boots it and waits for ssh. Expect one to three minutes.")
    for st in (s.get("setup") or []):
        out.append(f"Runs setup step: {st.get('name', 'unnamed')}.")
    if s["mode"] == "auto":
        for st in (s.get("steps") or []):
            out.append(f"Runs: {st.get('name', 'unnamed')}.")
    else:
        out.append("Then stops and hands over to you — nothing else is done automatically.")
    return out


def settle_sensor(host, progress=None, limit=120):
    """Wait for the sensor to finish initialising after a boot.

    `wait_ready` returns as soon as ssh answers, but the sensor needs another half minute or
    so, and until it does falconctl reports the PRE-BOOT state. Reverting from the RFM
    baseline back to a healthy one and checking immediately reports rfm-state=true on a host
    that is fine -- a stale reading that looks exactly like a failed revert.
    """
    say = progress or (lambda _m: None)
    import time as _t
    say("waiting for the sensor to initialise")
    prev, stable, t0 = None, 0, _t.time()
    while _t.time() - t0 < limit:
        r = sensor.verify(host)
        cur = (r.get("running"), r.get("rfm"), r.get("aid") is not None)
        stable = stable + 1 if cur == prev else 0
        prev = cur
        if stable >= 2:
            say(f"sensor settled: {r.get('verdict')}")
            return r
        _t.sleep(8)
    say("sensor did not settle within %ds; readings may be stale" % limit)
    return sensor.verify(host)


def prepare(sid, skip_revert=False, progress=None):
    s = get(sid)
    if s["target"] == "console" or s.get("baseline") == "none" or skip_revert:
        if progress:
            progress("no guest to prepare")
        return None
    r = core.revert(s["target"], s["baseline"], progress=progress)
    # Before the sensor settles, not after: the clock fix has to land before anything starts
    # timestamping, or the first detections of the exercise carry the snapshot's stale clock.
    if r.get("ready"):
        core.windows_post_revert(s["target"], progress=progress)
    if s["baseline"] in ("sensor", "rfm") and r.get("ready"):
        settle_sensor(s["target"], progress=progress)
    return r


def run(sid, skip_revert=False, progress=None):
    say = progress or (lambda _m: None)
    s = get(sid)
    result = {"scenario": sid, "name": s["name"], "target": s["target"],
              "mode": s["mode"], "domain": s.get("domain"), "steps": [],
              "expect": s.get("expect", {})}

    result["prepared"] = prepare(sid, skip_revert, progress=say)

    if s["mode"] in ("manual", "guided"):
        result["instructions"] = (s.get("instructions") or "").strip()

    # `guided` runs its setup steps and then hands over; `manual` runs nothing.
    steps = s.get("setup") if s["mode"] == "guided" else (
        s.get("steps") if s["mode"] == "auto" else [])
    for i, step in enumerate(steps or [], start=1):
        name = step.get("name", f"step {i}")
        # A `stage:` step runs LAB-side (API + guest together) rather than as a guest shell.
        # It exists for prerequisites that are not the lesson -- see lab/staging.py for the two
        # rules that constrain what may be staged.
        if step.get("stage"):
            say(f"step {i}: {name}")
            r = staging.run(step["stage"], s)
            result["steps"].append({"n": i, "name": name, "ok": r.get("ok"),
                                    "out": str(r.get("reason", ""))[:400]})
            if r.get("ok") is not True and step.get("required", True):
                result["failed_at"] = i
                break
            continue
        cmd = (step.get("shell") or "").strip()
        if not cmd:
            continue
        host = step.get("target") or (s["target"] if s["target"] != "console" else None)
        if not host:
            result["steps"].append({"n": i, "name": name, "ok": None,
                                    "out": "console scenario: no guest to run on"})
            continue
        say(f"step {i}: {name}")
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


def _check_api(s, v):
    """Read console CONFIG via the Falcon API (falconpy), not by scraping the browser.

    Structured and robust where the substring/DOM approach was fragile. Currently supports:
      assert: policy_group  policy: response|prevention|sensor_update  group: "<name>"
    """
    assertion = v.get("assert")
    if assertion in ("policy_group", "policy_assigned_to_group"):
        pk, grp = v.get("policy"), v.get("group")
        if not pk or not grp:
            return {"ok": None, "reason": "api policy_group check needs 'policy' and 'group'"}
        return falcon.policy_assigned_to_group(pk, grp)
    if assertion == "host_count":
        hn = v.get("hostname")
        if not hn:
            return {"ok": None, "reason": "api host_count check needs 'hostname'"}
        return falcon.visible_host_count_is(hn, v.get("equals", 1))
    if assertion == "host_group":
        grp = v.get("group")
        if not grp:
            return {"ok": None, "reason": "api host_group check needs 'group'"}
        return falcon.host_group_exists(grp)
    if assertion == "hosts_matching":
        pat = v.get("contains")
        if not pat:
            return {"ok": None, "reason": "api hosts_matching check needs 'contains'"}
        return falcon.visible_hosts_matching(pat, v.get("at_least", 1))
    if assertion == "ioc_exists":
        return falcon.custom_ioc_exists(v.get("value_contains"), v.get("type"), v.get("action"))
    if assertion == "ioa_group_enabled":
        grp = v.get("group")
        if not grp:
            return {"ok": None, "reason": "api ioa_group_enabled check needs 'group'"}
        return falcon.ioa_rule_group_enabled(grp)
    if assertion == "detection_contains":
        tok = v.get("contains")
        if not tok:
            return {"ok": None, "reason": "api detection_contains check needs 'contains'"}
        return falcon.recent_detection_contains(tok, v.get("within_min", 30))
    if assertion == "ml_exclusion":
        path = v.get("path_contains")
        if not path:
            return {"ok": None, "reason": "api ml_exclusion check needs 'path_contains'"}
        return falcon.ml_exclusion_exists(path, v.get("group"))
    if assertion == "rfm_state":
        # `target: win|lnx|mac` is the normal form -- it names the guest in lab terms, resolves
        # to that host's Falcon hostname, and is also what puts the right terminal button on the
        # scenario card. `hostname:` stays available for a host the lab does not own.
        hn = v.get("hostname")
        if not hn:
            t = v.get("target")
            if t not in config.HOSTS:
                return {"ok": None, "reason": "api rfm_state check needs 'target' (a lab guest) "
                                              "or 'hostname'"}
            hn = config.HOSTS[t]["name"]
        return falcon.rfm_state(hn, v.get("expect", "no"))
    if assertion == "host_contained":
        hn = v.get("hostname")
        if not hn:
            t = v.get("target")
            if t not in config.HOSTS:
                return {"ok": None, "reason": "api host_contained needs 'target' or 'hostname'"}
            hn = config.HOSTS[t]["name"]
        return falcon.host_contained(hn, v.get("expect", True))
    if assertion == "quarantined_file":
        hn = v.get("hostname")
        if not hn:
            return {"ok": None, "reason": "api quarantined_file check needs 'hostname'"}
        return falcon.quarantined_file(hn, v.get("name_contains"),
                                       within_min=v.get("within_min"))
    return {"ok": None, "reason": f"unknown api assert {assertion!r}"}


_CHECKERS = {"console": _check_console, "endpoint": _check_endpoint,
             "sensor": _check_sensor, "attest": _check_attest, "api": _check_api}


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
