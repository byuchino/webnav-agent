#!/usr/bin/env python3
"""Type text into a Proxmox VM's console via `qm sendkey`.

For the times a guest has no network yet and the only way in is the virtual keyboard —
which is exactly the bootstrap problem an unattended install is supposed to avoid, and
occasionally still leaves you with.

  ./tools/qm_type.py proxmox-1 900 --key ctrl-esc
  ./tools/qm_type.py proxmox-1 900 --text 'powershell' --key ret
"""
import argparse
import shlex
import subprocess
import sys

# QEMU key names for characters that are not simply themselves.
_PLAIN = {
    " ": "spc", "-": "minus", "=": "equal", "[": "bracket_left", "]": "bracket_right",
    "\\": "backslash", ";": "semicolon", "'": "apostrophe", ",": "comma", ".": "dot",
    "/": "slash", "`": "grave_accent",
}
# Characters produced by holding shift.
_SHIFTED = {
    ":": "semicolon", '"': "apostrophe", "<": "comma", ">": "dot", "?": "slash",
    "|": "backslash", "{": "bracket_left", "}": "bracket_right", "~": "grave_accent",
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8",
    "(": "9", ")": "0", "_": "minus", "+": "equal",
}


def keys_for(text):
    out = []
    for ch in text:
        if ch in _PLAIN:
            out.append(_PLAIN[ch])
        elif ch in _SHIFTED:
            out.append("shift-" + _SHIFTED[ch])
        elif ch.isdigit():
            out.append(ch)
        elif ch.isalpha():
            out.append(("shift-" + ch.lower()) if ch.isupper() else ch)
        else:
            raise ValueError(f"no key mapping for {ch!r}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("vmid")
    ap.add_argument("--text", action="append", default=[])
    ap.add_argument("--key", action="append", default=[])
    ap.add_argument("--delay-ms", type=int, default=45)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    keys = []
    for t in a.text:
        keys += keys_for(t)
    keys += a.key

    # One SSH round trip for the whole sequence; per-key ssh would take minutes.
    script = "; ".join(
        f"qm sendkey {a.vmid} {shlex.quote(k)} >/dev/null 2>&1; sleep {a.delay_ms / 1000:.3f}"
        for k in keys
    )
    if a.dry_run:
        print(f"{len(keys)} keys: {' '.join(keys)}")
        return 0
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", a.host, script], timeout=600)
    print(f"sent {len(keys)} keys")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
