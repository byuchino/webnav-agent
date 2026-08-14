#!/usr/bin/env python
"""Falcon lab control.

Built so routine work costs almost nothing to run or to read. Every command prints a few
aligned lines rather than a transcript, because the expensive part of driving this lab
through an assistant was never the compute -- it was the conversation.

  ./lab.py status
  ./lab.py revert win bare
  ./lab.py scenarios
  ./lab.py run eicar-win
  ./lab.py grade sensor-install-win
  ./lab.py sensor win
  ./lab.py web                      # control panel on :8901
"""
import argparse
import json
import sys

from lab import config, core, falcon, scenarios, sensor

OK, BAD, WARN, DIM = "\033[32m", "\033[31m", "\033[33m", "\033[2m"
END = "\033[0m"


def _c(s, colour):
    return f"{colour}{s}{END}" if sys.stdout.isatty() else str(s)


def _mark(v):
    if v is True:
        return _c("ok", OK)
    if v is False:
        return _c("FAIL", BAD)
    return _c("--", DIM)


def cmd_status(a):
    for name in config.GUESTS + ["dhcp"]:
        st = core.status(name)
        snaps = ",".join(st["snapshots"]) or "none"
        reach = _c("up", OK) if st["reachable"] else _c("down", BAD if st["vm"] == "running" else DIM)
        line = f"{name:5} {st['ip']:12} {st['vm']:8} {reach:12}"
        if name in config.GUESTS:
            s = sensor.verify(name)
            line += f" sensor: {s.get('verdict', '?')}"
        print(line)
        if a.verbose:
            print(f"      snapshots: {snaps}")


def cmd_revert(a):
    r = core.revert(a.host, a.baseline)
    ready = "ready" if r["ready"] else "NOT reachable"
    print(f"{a.host:5} -> {r['snapshot']}  {ready} ({r.get('seconds', '?')}s)")


def cmd_start(a):
    print(f"{a.host:5} {core.vm_start(a.host)}")


def cmd_stop(a):
    print(f"{a.host:5} {core.vm_stop(a.host)}")


def cmd_snapshot(a):
    core.snapshot_create(a.host, a.name, a.description or "", live=a.live)
    how = "live (NOT a reliable rollback point for Windows)" if a.live else "clean shutdown"
    print(f"{a.host:5} snapshot {a.name} taken ({how})")


DOMAINS = {
    0: "Foundations", 1: "User Management", 2: "Sensor Deployment",
    3: "Host Management", 4: "Group Creation", 5: "Policy Application",
    6: "Rules Configuration", 7: "Dashboards and Reports", 8: "Workflows",
}


def cmd_scenarios(a):
    all_ = scenarios.load_all()
    last = None
    for s in sorted(all_.values(), key=lambda x: (x.get("domain", 0), x["id"])):
        d = s.get("domain", 0)
        if d != last:
            print(f"\n{_c(f'{d}. ' + DOMAINS.get(d, '?'), WARN)}")
            last = d
        mode = _c(f"{s['mode']:6}", WARN) if s["mode"] != "auto" else f"{s['mode']:6}"
        kinds = ",".join(sorted({v["kind"] for v in (s.get("verify") or [])})) or "-"
        print(f"  {s['id']:28} {s['target']:7} {mode} {kinds:22} {s['name'][:38]}")


def cmd_show(a):
    s = scenarios.get(a.scenario)
    print(f"{s['id']}  ({s['name']})")
    print(f"  target   {s['target']}   baseline {s['baseline']}   mode {s['mode']}")
    if s.get("syllabus"):
        print(f"  syllabus {s['syllabus']}")
    print(f"\n{(s.get('summary') or '').strip()}\n")
    if s.get("teaches"):
        print("Teaches:")
        print(s["teaches"].rstrip())
    if s.get("instructions"):
        print("\nInstructions:")
        print(s["instructions"].rstrip())
    e = s.get("expect") or {}
    if e:
        print("\nExpect:")
        for k, v in e.items():
            print(f"  {k}: {str(v).strip()}")


def cmd_run(a):
    r = scenarios.run(a.scenario, skip_revert=a.no_revert)
    prep = r.get("prepared")
    if prep:
        print(f"{r['scenario']}  target={r['target']}  baseline -> {prep['snapshot']} "
              f"({'ready' if prep['ready'] else 'NOT READY'})")
    else:
        print(f"{r['scenario']}  target={r['target']}  (no revert)")

    if r["mode"] == "manual":
        print(f"\n{r['instructions']}\n")
        print(_c(f"When you are done:  ./lab.py grade {r['scenario']}", WARN))
        return

    for st in r["steps"]:
        print(f"  [{st['n']}] {st['name']:44} {_mark(st['ok'])}")
        if st["out"] and (a.verbose or st["ok"] is False):
            for line in st["out"].splitlines()[:6]:
                print(f"        {_c(line, DIM)}")
    e = r.get("expect") or {}
    if e.get("console"):
        print(f"\nLook in Falcon: {str(e['console']).strip()}")


def cmd_grade(a):
    r = scenarios.grade(a.scenario)
    label = (_c("PASS", OK) if r["passed"] is True else
             _c("NOT YET", BAD) if r["passed"] is False else _c("UNVERIFIED", DIM))
    print(f"{r['scenario']:28} {label}")
    for c in r.get("checks", []):
        mark = _mark(c["ok"])
        print(f"  {mark:14} {c['label']:34} {c.get('reason','')[:60]}")
        for item in c.get("items", []):
            print(f"        {_c('[ ] ' + item, DIM)}")
    if r["passed"] is not True and r.get("hint"):
        print()
        print(r["hint"].rstrip())


def cmd_sensor(a):
    if a.action == "verify":
        for name in ([a.host] if a.host else config.GUESTS):
            r = sensor.verify(name)
            print(f"{name:5} {r.get('verdict', '?')}")
            if a.verbose:
                for k in ("service", "version", "cid", "aid", "rfm"):
                    v = r.get(k)
                    if v is None:
                        continue
                    # The CID identifies the tenant and is what lets a sensor register into
                    # it; the AID identifies the host. Neither belongs in a terminal, a log
                    # or a pasted transcript. Confirm they are set, do not reproduce them.
                    if k == "cid":
                        v = "set" if v else "not set"
                    elif k == "aid":
                        v = f"set ({str(v)[:6]}...)"
                    print(f"      {k:8} {v}")
    elif a.action == "installers":
        found = sensor.installers()
        print("\n".join(f"  {f}" for f in found) if found else
              f"  none staged. Download from the Falcon console into {config.INSTALLER_DIR}")
    elif a.action == "stage":
        if not a.host:
            sys.exit("sensor stage needs a host, e.g. ./lab.py sensor stage lnx")
        r = sensor.stage(a.host, a.installer)
        print(f"{a.host:5} staged {r['installer']}")
        print(f"      {r['remote']}  -- install it yourself from the panel's terminal")
    elif a.action == "install":
        cid = a.ccid or config.ccid()
        if not cid:
            sys.exit(f"no CID. Put it in {config.CCID_FILE} (see docs/lab-cli.md) "
                     f"rather than passing it on the command line.")
        r = sensor.install(a.host, cid, a.installer)
        print(f"{a.host:5} installed {r['installer']} rc={r['rc']}")
        print(f"      {sensor.verify(a.host).get('verdict')}")
    elif a.action == "remove":
        r = sensor.remove(a.host, a.token)
        print(f"{a.host:5} remove rc={r['rc']}")


POLICY_LABELS = {"prevention": "Prevention", "sensor_update": "Sensor update",
                 "response": "Response"}


def cmd_policies(a):
    kinds = [a.kind] if a.kind else list(falcon.VALID_POLICY_KINDS)
    matrix = falcon.policy_matrix(kinds)

    if a.by_group:
        # group -> [(kind, policy)]. The inverted view answers "what hits this group?", which is
        # the question a policy exercise actually asks and the console makes you assemble by hand.
        by_group, broken = {}, []
        for kind, res in matrix.items():
            if res["ok"] is not True:
                broken.append(f"{kind}: {res['reason']}")
                continue
            for p in res["policies"]:
                for g in p["groups"]:
                    by_group.setdefault(g, []).append((kind, p))
        for g in sorted(by_group):
            print(_c(g, WARN))
            for kind, p in by_group[g]:
                state = _c("enabled", OK) if p["enabled"] else _c("disabled", DIM)
                print(f"  {POLICY_LABELS.get(kind, kind):14} {p['name'][:34]:34} "
                      f"{p['platform']:8} {state}")
        if not by_group and not broken:
            print(_c("no policy is assigned to any host group", DIM))
        for b in broken:
            print(_c(f"  -- {b}", BAD))
        return

    for kind in kinds:
        res = matrix[kind]
        print(_c(f"\n{POLICY_LABELS.get(kind, kind)} policies", WARN))
        if res["ok"] is not True:
            print(_c(f"  -- {res['reason']}", BAD))
            continue
        if res["reason"]:
            print(_c(f"  !! {res['reason']}", BAD))
        for p in res["policies"]:
            state = _c("enabled", OK) if p["enabled"] else _c("disabled", DIM)
            groups = ", ".join(p["groups"])
            if p["default"]:
                groups = _c("(catch-all: every host no policy above claimed)", DIM)
            elif not groups:
                groups = _c("no groups -- inert", BAD)
            print(f"  {p['name'][:32]:32} {p['platform']:8} {state:16} {groups}")
    print(_c("\nlisted in precedence order: first policy whose group matches a host wins",
             DIM))


def cmd_ioc(a):
    """Lab-owned IOCs only. `clean` can only ever remove what the lab tagged."""
    if a.action == "list":
        r = falcon.custom_ioc_exists()
        print(f"  {r.get('reason')}")
        d = falcon.ioc_clean(dry_run=True)
        print(f"  lab-owned: {d.get('reason')}")
    elif a.action == "add":
        if not a.value:
            sys.exit("ioc add needs a value, e.g. ./lab.py ioc add <sha256>")
        r = falcon.ioc_create(a.value, a.type, a.action_on, host_groups=[a.group] if a.group
                              else None, description=a.description or "")
        print(f"  {_mark(r.get('ok'))} {r.get('reason')}")
    elif a.action == "clean":
        r = falcon.ioc_clean(dry_run=a.dry_run)
        print(f"  {_mark(r.get('ok'))} {r.get('reason')}")


def cmd_web(a):
    import uvicorn
    from lab.web import app
    # Loopback by default: the panel has no auth of its own, and /api/term is a shell on the
    # guests. Anything that publishes it -- a tunnel, a reverse proxy -- must put auth in front,
    # and those all connect from localhost anyway. Binding every interface adds a second,
    # unguarded door on the LAN that bypasses whatever is guarding the first one.
    print(f"lab panel on http://{a.host}:{a.port}")
    if a.host != "127.0.0.1":
        print(f"  !! {a.host} exposes an unauthenticated admin shell to that network")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(fn=cmd_status)

    r = sub.add_parser("revert", help="roll a guest back to a baseline")
    r.add_argument("host", choices=config.GUESTS)
    r.add_argument("baseline", nargs="?", default="bare", choices=config.BASELINES)
    r.set_defaults(fn=cmd_revert)

    for verb, fn in (("start", cmd_start), ("stop", cmd_stop)):
        s = sub.add_parser(verb)
        s.add_argument("host", choices=list(config.HOSTS))
        s.set_defaults(fn=fn)

    s = sub.add_parser("snapshot")
    s.add_argument("host", choices=config.GUESTS)
    s.add_argument("name")
    s.add_argument("-d", "--description")
    s.add_argument("--live", action="store_true",
                   help="snapshot without shutting down; faster, but a live Windows snapshot "
                        "can capture NTFS mid-write and produce an unbootable rollback")
    s.set_defaults(fn=cmd_snapshot)

    sub.add_parser("scenarios", help="list scenarios").set_defaults(fn=cmd_scenarios)

    s = sub.add_parser("show", help="full text of one scenario")
    s.add_argument("scenario")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("run", help="prepare and run a scenario")
    s.add_argument("scenario")
    s.add_argument("--no-revert", action="store_true", help="do not roll back first")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("grade", help="check whether the goal was achieved")
    s.add_argument("scenario")
    s.set_defaults(fn=cmd_grade)

    s = sub.add_parser("sensor")
    s.add_argument("action", choices=["verify", "stage", "install", "remove", "installers"],
                   nargs="?", default="verify")
    s.add_argument("host", nargs="?", choices=config.GUESTS)
    s.add_argument("--ccid", help="CID with checksum")
    s.add_argument("--installer")
    s.add_argument("--token", help="maintenance token, for uninstall")
    s.set_defaults(fn=cmd_sensor)

    s = sub.add_parser("policies", help="which policies are assigned to which host groups")
    s.add_argument("kind", nargs="?", choices=list(falcon.VALID_POLICY_KINDS))
    s.add_argument("--by-group", action="store_true", help="invert: group -> policies")
    s.set_defaults(fn=cmd_policies)

    s = sub.add_parser("ioc", help="lab-owned custom IOCs (needs IOC Management write to add)")
    s.add_argument("action", choices=["list", "add", "clean"], nargs="?", default="list")
    s.add_argument("value", nargs="?", help="the indicator, e.g. a sha256")
    s.add_argument("--type", default="sha256")
    s.add_argument("--action-on", default="prevent", choices=list(falcon.VALID_IOC_ACTIONS),
                   help="what the sensor does on a match (default prevent)")
    s.add_argument("--group", help="host group to scope it to (default: applied globally)")
    s.add_argument("--description")
    s.add_argument("--dry-run", action="store_true", help="clean: show what would go")
    s.set_defaults(fn=cmd_ioc)

    s = sub.add_parser("web", help="serve the control panel")
    s.add_argument("--port", type=int, default=8901)
    s.add_argument("--host", default="127.0.0.1",
                   help="bind address (default loopback; 0.0.0.0 exposes an unauthenticated "
                        "admin shell to the whole network)")
    s.set_defaults(fn=cmd_web)

    a = p.parse_args()
    try:
        a.fn(a)
    except (core.LabError, KeyError, ValueError) as e:
        sys.exit(_c(f"error: {e}", BAD))
    return 0


if __name__ == "__main__":
    sys.exit(main())
