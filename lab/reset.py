"""What an exercise left behind in the CID, and how to put it back.

The lab resets a GUEST in forty seconds with a snapshot. The console has no equivalent, and
that asymmetry is the problem this module exists to narrow: after running the Domain 5 and 6
exercises the CID carries duplicated policies, a rule group, a flipped response-policy toggle
and a couple of quarantine records, and none of it announces itself. The next person to run
those exercises does not start where the last one did.

**This module is READ-ONLY about console configuration.** It reports; it does not delete. The
API key holds write scope for IOC Management and nothing else, deliberately: a prevention-policy
write can silently edit `platform_default` or a stock `Phase N` template that every other
exercise depends on, and nothing would announce that either. So the honest thing is a checklist
that verifies itself rather than a teardown that might overreach.

What it CAN do it does: `ioc_clean()` removes lab-tagged IOCs, and a guest revert is a snapshot
away. Everything else prints as a step for a human, with the console path.

Identifying "lab-created" is a judgement, so it is made explicit rather than guessed: an object
counts if its name starts with LAB_PREFIX, or if it is assigned to the lab's host group. Both
are reported with the reason, and nothing is ever asserted to be safe to delete -- the operator
decides.
"""
from . import config, falcon

LAB_PREFIX = "Falcon Lab"
LAB_GROUP = "Falcon Lab"

# Response-policy commands the lab's documented baseline leaves OFF. `rtr-run-binaries` turns
# `run` on as its whole point, so finding it enabled is expected after that exercise and is
# still drift that the next learner should not inherit.
RESPONSE_BASELINE_OFF = ("run", "put-and-run")


def _classify(name, groups):
    """CREATED by an exercise, or merely pre-existing and IN SCOPE? Only the first may be
    deleted — and the distinction is not cosmetic. `RTR Lab (Windows)` is assigned to the lab's
    host group and is part of the lab's own furniture; an early version of this reporter said
    "detach, then delete" about it, which would have taken out the response policy every RTR
    exercise depends on. Group membership means "an exercise touched this", never "an exercise
    made this".
    """
    if (name or "").startswith(LAB_PREFIX):
        return "created", f"name starts with {LAB_PREFIX!r}"
    if LAB_GROUP in (groups or []):
        return "in-scope", f"pre-existing, assigned to the {LAB_GROUP!r} host group"
    return None, None


def inventory():
    """Everything an exercise plausibly left behind. Returns a list of findings:
    {area, what, detail, action} — `action` is the console step, or None if the lab can do it."""
    clients = falcon._clients()
    if not clients:
        return [{"area": "api", "what": "cannot look", "detail": falcon._why_unavailable(),
                 "action": None}]
    out = []

    for kind, svc in (("prevention", "prevention"), ("response", "response"),
                      ("sensor update", "sensor_update")):
        try:
            r = clients[svc].query_combined_policies(limit=500)
        except Exception as e:  # noqa: BLE001
            out.append({"area": f"{kind} policies", "what": "read failed",
                        "detail": str(e)[:100], "action": None})
            continue
        for p in (r.get("body") or {}).get("resources") or []:
            name = p.get("name") or ""
            if name in falcon.NEVER_TOUCH:
                continue
            groups = [g.get("name") if isinstance(g, dict) else g for g in (p.get("groups") or [])]
            origin, why = _classify(name, groups)
            if not origin:
                continue
            bits = [f"platform={p.get('platform_name')}", f"enabled={p.get('enabled')}"]
            if groups:
                bits.append(f"groups={groups}")
            rgs = [g.get("name") for g in (p.get("ioa_rule_groups") or []) if isinstance(g, dict)]
            if rgs:
                bits.append(f"ioa_rule_groups={rgs}")
            if origin == "created":
                # A policy still holding a group or a rule group cannot simply be deleted, and
                # the order matters more than the deletion does -- say so here, not in a doc.
                detach = (["host groups"] if groups else []) + (["custom IOAs"] if rgs else [])
                act = (f"detach first ({' and '.join(detach)}), then delete" if detach
                       else "delete")
                out.append({"area": f"{kind} policy", "what": name,
                            "detail": f"{', '.join(bits)}  [{why}]",
                            "action": f"Endpoint security > {kind.title()} policies: {act}"})
            # else: pre-existing lab furniture. Reported only if it has drifted, below -- never
            # proposed for deletion.

            if svc == "response":
                on = [st.get("name") for g in (p.get("settings") or [])
                      for st in (g.get("settings") or [])
                      if (st.get("value") or {}).get("enabled")
                      and st.get("name") in RESPONSE_BASELINE_OFF]
                if on:
                    out.append({"area": "response policy drift", "what": name,
                                "detail": f"high-risk command(s) enabled: {', '.join(on)} "
                                          f"(lab baseline leaves these off)",
                                "action": "Response policies > High risk commands: untick, Save"})

    try:
        q = clients["custom_ioa"].query_rule_groups_full(limit=200)
        for g in (q.get("body") or {}).get("resources") or []:
            if not (g.get("name") or "").startswith(LAB_PREFIX):
                continue
            out.append({"area": "IOA rule group", "what": g.get("name"),
                        "detail": f"platform={g.get('platform')} enabled={g.get('enabled')} "
                                  f"rules={len(g.get('rules') or [])}",
                        "action": "Custom IOA rule groups: unassign from any policy, then delete"})
    except Exception as e:  # noqa: BLE001
        out.append({"area": "IOA rule groups", "what": "read failed", "detail": str(e)[:100],
                    "action": None})

    d = falcon.ioc_clean(dry_run=True)
    if (d.get("reason") or "").startswith("would delete"):
        out.append({"area": "IOCs", "what": "lab-owned indicators", "detail": d["reason"],
                    "action": None})  # None = the lab can do this one

    try:
        qq = clients["quarantine"].query_quarantine_files(limit=200)
        n = len((qq.get("body") or {}).get("resources") or [])
        if n:
            out.append({"area": "quarantined files", "what": f"{n} record(s)",
                        "detail": "records outlive the file AND the IOC that caused them, so "
                                  "they satisfy a later run's check unless cleared",
                        "action": "Endpoint security > Monitor > Quarantined files: select, "
                                  "Delete"})
    except Exception:  # noqa: BLE001
        pass

    for g in config.GUESTS:
        out.append({"area": "guest", "what": g,
                    "detail": "exercise artifacts on disk (test binaries, quarantine store)",
                    "action": None})
    return out


def apply_safe():
    """Do only what the lab can do without console write scope. Returns a list of result lines."""
    done = []
    r = falcon.ioc_clean()
    done.append(f"IOCs: {r.get('reason')}")
    done.append("guests: run `./lab.py revert <host> sensor` to discard on-disk artifacts "
                "(not done automatically -- it destroys whatever is on the guest now)")
    return done
