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

from lab import config, core, scenarios, sensor

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


def cmd_scenarios(a):
    all_ = scenarios.load_all()
    for sid in sorted(all_):
        s = all_[sid]
        mode = _c("manual", WARN) if s["mode"] == "manual" else "auto"
        print(f"{sid:22} {s['target']:4} {s['baseline']:7} {mode:8} {s['name']}")


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
    verdict = r.get("verdict", "")
    if r["passed"] is True:
        print(f"{r['scenario']:22} {_c('PASS', OK)}  {verdict}")
    elif r["passed"] is False:
        print(f"{r['scenario']:22} {_c('NOT YET', BAD)}  {verdict}")
        if r.get("hint"):
            print(r["hint"].rstrip())
    else:
        print(f"{r['scenario']:22} {_c('--', DIM)}  {verdict}")
    if a.verbose and r.get("detail"):
        print(json.dumps(r["detail"], indent=2))


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


def cmd_web(a):
    import uvicorn
    from lab.web import app
    print(f"lab panel on http://0.0.0.0:{a.port}")
    uvicorn.run(app, host="0.0.0.0", port=a.port, log_level="warning")


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
    s.add_argument("action", choices=["verify", "install", "remove", "installers"],
                   nargs="?", default="verify")
    s.add_argument("host", nargs="?", choices=config.GUESTS)
    s.add_argument("--ccid", help="CID with checksum")
    s.add_argument("--installer")
    s.add_argument("--token", help="maintenance token, for uninstall")
    s.set_defaults(fn=cmd_sensor)

    s = sub.add_parser("web", help="serve the control panel")
    s.add_argument("--port", type=int, default=8901)
    s.set_defaults(fn=cmd_web)

    a = p.parse_args()
    try:
        a.fn(a)
    except (core.LabError, KeyError, ValueError) as e:
        sys.exit(_c(f"error: {e}", BAD))
    return 0


if __name__ == "__main__":
    sys.exit(main())
