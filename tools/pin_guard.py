#!/usr/bin/env python
"""Re-assert the Windows guest's update pin and clock. Run on a timer, not just on revert.

`scenarios.prepare()` calls `core.windows_post_revert()` at every exercise start, which is
enough for a guest that is reverted often. It is NOT enough for one left running: measured
2026-08-18 after four days of uptime, Windows had walked wuauserv 4 -> 3, UsoSvc 4 -> 2 and
WaaSMedicSvc 4 -> 3 on its own, and staged five updates. UBR had not moved only because nothing
had rebooted -- and Windows applies staged updates AT SHUTDOWN, so the guest was one reboot away
from landing past the sensor's supported build and into RFM.

The scheduled tasks that do this live under \\Microsoft\\Windows\\UpdateOrchestrator, are
SYSTEM-owned, and cannot be disabled as labadmin. So this does not try to win; it just re-asserts
often enough that the window between erosion and a shutdown stays small.

Exits 0 whether or not the guest was reachable: a lab guest that is powered off is normal, and a
timer that reports failure for it would train you to ignore the timer.
"""
import sys

sys.path.insert(0, "/opt/falcon-lab")

from lab import config, core  # noqa: E402


def main():
    for name in config.GUESTS:
        if config.HOSTS[name]["os"] != "windows":
            continue
        if not core.reachable(name):
            print(f"{name}: not reachable, skipped")
            continue
        try:
            r = core.windows_post_revert(name)
            print(f"{name}: ok={r.get('ok')} ubr={r.get('ubr')}")
        except Exception as e:  # noqa: BLE001
            print(f"{name}: FAILED {type(e).__name__}: {str(e)[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
