"""Reading Falcon console state.

This is the seam. Everything that talks to the console goes through here, so the eventual
decision about how `falcon-lab` depends on the agent — vendor the modules, or take the agent
as a package — touches this file and nothing else.

## How it reads

Not by scraping the page. The console is a single-page app that renders a view of JSON it
already fetched, so the honest thing to read is the JSON. `observe.NetworkRecorder` captures
the API responses the browser receives, which is both more faithful than the DOM and far more
stable than a rendered layout.

That is the same mechanism that got the documentation out of a portal whose article body sat
in an iframe with twenty characters of visible text. It also means grading survives a UI
redesign that would break any selector-based approach.

## What it needs

A Chrome signed in to the console, reachable over CDP (`CDP_PORT`, typically an SSH tunnel to
the machine holding the session). It **reads only**: navigate and observe, never click. The
session expires, so a check that cannot find its data says so rather than reporting a
failure — "I could not look" and "I looked and it was wrong" are different answers and the
grader must not conflate them.
"""
import asyncio
import os
import sys

# The agent modules live one level up until the repo split; this import is the whole
# dependency surface between the lab and the agent.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import cdp, snapshot  # noqa: E402
from observe import NetworkRecorder  # noqa: E402

CONSOLE = os.environ.get("FALCON_CONSOLE", "https://falcon.us-2.crowdstrike.com")

# The console's own API prefix. Discovered by capturing traffic, not guessed: the first
# version of this used "/api/" and every check silently captured zero bodies.
API_PREFIX = os.environ.get("FALCON_API_PREFIX", "/api2/")


class ConsoleUnavailable(RuntimeError):
    """No usable console session. Distinct from a failed assertion on purpose."""


def url_for(path):
    if path.startswith("http"):
        return path
    return CONSOLE.rstrip("/") + "/" + path.lstrip("/")


async def _read(path, api_match, settle=14, max_wait=40):
    """Navigate to a console page and return the API responses it fetched."""
    tid, ws = await cdp.open_url("about:blank")
    try:
        async with cdp.Client(ws) as c:
            await asyncio.sleep(0.5)
            net = NetworkRecorder()
            net.attach(c)
            await c.send("Network.enable", {})
            await c.send("Page.navigate", {"url": url_for(path)})

            waited, seen, stable = 0, 0, 0
            while waited < max_wait:
                await asyncio.sleep(2)
                waited += 2
                await net.fetch_bodies(c, match=api_match or API_PREFIX, limit=60)
                now = len(net.bodies(match=api_match, min_len=2))
                stable = stable + 1 if now == seen else 0
                seen = now
                if waited >= settle and stable >= 2 and now:
                    break

            title = await cdp.evaluate(c, "document.title") or ""
            if "Login" in title or "/login" in (await cdp.evaluate(c, "location.href") or ""):
                raise ConsoleUnavailable(
                    "the console session has expired — sign in again in the browser "
                    f"({CONSOLE}) and re-run")
            return net.bodies(match=api_match, min_len=2), title
    finally:
        await cdp.close_target(tid)


def read(path, api_match, settle=14):
    """Sync wrapper. Returns (bodies, page_title)."""
    return asyncio.run(_read(path, api_match, settle))


def check(path, api_match=None, expect_contains=None, expect_absent=None, settle=14):
    """Assert on what the console's own API returned.

    Deliberately substring matching rather than a JSON path language: the console's internal
    API is undocumented and unversioned, so its response *shape* is the least stable thing
    about it. The presence of a host group's name or a policy's name in the payload is a far
    more durable assertion than the key path it happens to sit under this month.

    Returns {ok, reason, matched, bodies_seen}. `ok is None` means the check could not be
    performed — never treat that as a pass.
    """
    try:
        bodies, title = read(path, api_match, settle)
    except ConsoleUnavailable as e:
        return {"ok": None, "reason": str(e), "bodies_seen": 0}
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "reason": f"console read failed: {str(e)[:120]}", "bodies_seen": 0}

    if not bodies:
        return {"ok": None, "bodies_seen": 0,
                "reason": f"no API response matching {api_match!r} on {path} — "
                          f"the page may have changed, or the filter is wrong"}

    blob = "\n".join(b["body"] for b in bodies)
    detail = {"bodies_seen": len(bodies), "bytes": len(blob)}

    if expect_contains is not None:
        want = expect_contains if isinstance(expect_contains, list) else [expect_contains]
        missing = [w for w in want if w not in blob]
        if missing:
            return {"ok": False, "reason": f"not found in the console's response: "
                                           f"{', '.join(repr(m) for m in missing)}", **detail}
    if expect_absent is not None:
        bad = expect_absent if isinstance(expect_absent, list) else [expect_absent]
        present = [b for b in bad if b in blob]
        if present:
            return {"ok": False, "reason": f"should not be present but is: "
                                           f"{', '.join(repr(p) for p in present)}", **detail}
    return {"ok": True, "reason": "console state matches", **detail}


def available():
    """Is there a usable console session right now?"""
    try:
        r = check("/host-management/hosts", API_PREFIX, settle=8)
        return r["ok"] is not None or "expired" not in (r.get("reason") or "")
    except Exception:  # noqa: BLE001
        return False


# --- API-backed reads (falconpy) ------------------------------------------------------
# Reading console CONFIG (host groups, policy assignments) by capturing browser traffic and
# substring-matching it is fragile: policies reference groups by ID, so the group NAME the
# check wants is never in the policy payload (confirmed against falconpy's own data model --
# assigning a policy takes a group_id, and names live on the host-groups service). The Falcon
# API answers these questions directly. It is READ-ONLY and optional; with no key configured,
# these checks report `ok=None` ("could not look"), never a false failure.
from . import config  # noqa: E402

_API = {}


def _clients():
    """Cached falconpy service objects, or None if unconfigured / falconpy missing.
    One shared OAuth2 token across the services keeps auth to a single round trip."""
    if "c" in _API:
        return _API["c"]
    creds = config.api_creds()
    if not creds:
        _API["c"] = None
        _API["why"] = ("no Falcon API key configured -- set FALCON_CLIENT_ID/SECRET or "
                       f"{config.API_CREDS_FILE} (read-only Host Groups + Policies scopes)")
        return None
    cid, sec, cloud = creds
    try:
        from falconpy import (OAuth2, HostGroup, ResponsePolicies, PreventionPolicy,
                              SensorUpdatePolicy, Hosts, CustomIOA, Alerts, MLExclusions,
                              Quarantine, IOC)
    except ImportError:
        _API["c"] = None
        _API["why"] = "falconpy is not installed (pip install crowdstrike-falconpy)"
        return None
    try:
        auth = OAuth2(client_id=cid, client_secret=sec, base_url=cloud)
        _API["c"] = {"_hg": HostGroup(auth_object=auth),
                     "hosts": Hosts(auth_object=auth),
                     "custom_ioa": CustomIOA(auth_object=auth),
                     "alerts": Alerts(auth_object=auth),
                     "ml_exclusions": MLExclusions(auth_object=auth),
                     "quarantine": Quarantine(auth_object=auth),
                     "ioc": IOC(auth_object=auth),
                     "response": ResponsePolicies(auth_object=auth),
                     "prevention": PreventionPolicy(auth_object=auth),
                     "sensor_update": SensorUpdatePolicy(auth_object=auth)}
    except Exception as e:  # noqa: BLE001
        _API["c"] = None
        _API["why"] = f"falconpy auth setup failed: {str(e)[:100]}"
    return _API["c"]


def _why_unavailable():
    return _API.get("why", "Falcon API unavailable")


def _group_ids(groups):
    """A policy's `groups` may be a list of ID strings or of {id,...} objects. Normalise."""
    out = set()
    for g in groups or []:
        if isinstance(g, str):
            out.add(g)
        elif isinstance(g, dict) and g.get("id"):
            out.add(g["id"])
    return out


def _resolve_group_id(clients, name):
    """Group NAME -> id via the host-groups service.

    Matches client-side rather than with an FQL `name:'...'` filter: server-side name matching
    on values with spaces proved unreliable (it returned no rows for a group that plainly
    exists), and a CID has few enough host groups that fetching them and comparing names is both
    simpler and exact.
    """
    r = clients["_hg"].query_combined_host_groups(limit=500)
    if r.get("status_code") != 200:
        raise ConsoleUnavailable(f"host-groups read failed (HTTP {r.get('status_code')}) -- "
                                 f"check the key's Host Groups read scope")
    for res in (r.get("body") or {}).get("resources") or []:
        if (res.get("name") or "") == name:
            return res.get("id")
    return None


VALID_POLICY_KINDS = ("response", "prevention", "sensor_update")


def policy_assigned_to_group(policy_kind, group_name):
    """Is a <policy_kind> policy assigned to the host group named <group_name>?

    Returns {ok, reason}. `ok is None` = could not look (no key / auth / scope) -- never a pass.
    Resolves the group name to an ID, then checks each policy's assigned groups for that ID --
    the correct two-step the substring grader could not do.
    """
    clients = _clients()
    if not clients:
        return {"ok": None, "reason": _why_unavailable()}
    if policy_kind not in clients:
        return {"ok": None, "reason": f"unknown policy kind {policy_kind!r}"}
    try:
        gid = _resolve_group_id(clients, group_name)
        if gid is None:
            return {"ok": False, "reason": f"no host group named {group_name!r} exists"}
        r = clients[policy_kind].query_combined_policies(limit=500)
        if r.get("status_code") != 200:
            return {"ok": None, "reason": f"{policy_kind}-policy read failed "
                    f"(HTTP {r.get('status_code')}) -- check the key's read scope for it"}
        for p in (r.get("body") or {}).get("resources") or []:
            if gid in _group_ids(p.get("groups")):
                return {"ok": True, "reason": f"{policy_kind} policy {p.get('name')!r} "
                        f"is assigned to {group_name!r}"}
        return {"ok": False, "reason": f"no {policy_kind} policy is assigned to "
                f"the {group_name!r} group"}
    except ConsoleUnavailable as e:
        return {"ok": None, "reason": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "reason": f"Falcon API read failed: {str(e)[:120]}"}


def _group_names(groups):
    """A policy's `groups` entries carry the name when the API expands them; fall back to id."""
    out = []
    for g in groups or []:
        if isinstance(g, str):
            out.append(g)
        elif isinstance(g, dict):
            out.append(g.get("name") or g.get("id") or "?")
    return out


def policy_matrix(kinds=VALID_POLICY_KINDS):
    """Every policy of each kind, in precedence order, with the groups it is assigned to.

    This is the view the console does not give you in one place: the console lists policies per
    platform tab and makes you open each one to see its groups, and lists a host group without
    saying which policies target it. Returns
    {kind: {"ok": bool|None, "reason": str, "policies": [{name, platform, enabled, default,
    groups: [...]}]}}, ordered the way the sensor resolves them -- first match wins, and the
    platform default at the bottom catches whatever nothing else claimed.
    """
    clients = _clients()
    if not clients:
        return {k: {"ok": None, "reason": _why_unavailable(), "policies": []} for k in kinds}
    out = {}
    for kind in kinds:
        if kind not in clients:
            out[kind] = {"ok": None, "reason": f"unknown policy kind {kind!r}", "policies": []}
            continue
        try:
            # precedence.asc is what the sensor actually walks. If the sort key is rejected the
            # unsorted list is still worth showing -- flagged, so nobody reads order into it.
            r = clients[kind].query_combined_policies(limit=500, sort="precedence.asc")
            ordered = r.get("status_code") == 200
            if not ordered:
                r = clients[kind].query_combined_policies(limit=500)
            if r.get("status_code") != 200:
                out[kind] = {"ok": None, "policies": [],
                             "reason": f"read failed (HTTP {r.get('status_code')}) -- check the "
                                       f"key's read scope for {kind} policies"}
                continue
            pols = []
            for p in (r.get("body") or {}).get("resources") or []:
                groups = _group_names(p.get("groups"))
                pols.append({"name": p.get("name") or "?",
                             "platform": p.get("platform_name") or "?",
                             "enabled": bool(p.get("enabled")),
                             # The platform default has no groups and cannot be given any; it is
                             # the catch-all, not an unassigned policy, and saying so avoids the
                             # obvious misreading of an empty group list.
                             "default": (p.get("name") or "").endswith("platform_default"),
                             "groups": groups})
            out[kind] = {"ok": True, "policies": pols,
                         "reason": "" if ordered else "NOT in precedence order (sort rejected)"}
        except Exception as e:  # noqa: BLE001
            out[kind] = {"ok": None, "policies": [],
                         "reason": f"Falcon API read failed: {str(e)[:120]}"}
    return out


def visible_host_count_is(hostname, equals):
    """Are there exactly `equals` VISIBLE (non-hidden) hosts with this hostname?

    Hidden hosts are excluded from device queries, so this actually verifies a dedup: a stale
    duplicate that was hidden no longer counts. Needs the key's Hosts read scope.
    """
    clients = _clients()
    if not clients:
        return {"ok": None, "reason": _why_unavailable()}
    try:
        r = clients["hosts"].query_devices_by_filter(filter=f"hostname:'{hostname}'", limit=500)
        if r.get("status_code") != 200:
            return {"ok": None, "reason": f"devices read failed (HTTP {r.get('status_code')}) -- "
                    f"check the key's Hosts read scope"}
        n = len(((r.get("body") or {}).get("resources")) or [])
        return {"ok": n == equals,
                "reason": f"{n} visible host(s) named {hostname!r} (expected {equals})"}
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "reason": f"Falcon API read failed: {str(e)[:120]}"}


def rfm_state(hostname, expect="no"):
    """Is this host in Reduced Functionality Mode, per the CLOUD? (Hosts read scope.)

    The only PLATFORM-AGNOSTIC way to ask. Host-side, every OS answers differently and two of
    the three barely answer at all: Linux has `falconctl -g --rfm-state`, macOS has its own
    falconctl under Falcon.app, and Windows has NO host-side RFM query whatsoever (verified on
    the lab guest -- CSSensorSettings.exe does proxy, grouping tags and no-rtr, nothing else,
    and `sc query csagent` reporting RUNNING does not mean the sensor is out of RFM). The
    sensor reports its state up regardless, so the device record answers for all three.

    `reduced_functionality_mode` is yes/no/unknown; `unknown` grades as None, not a fail --
    an older sensor that never populated the field is not evidence of a problem.

    `expect` is `no`, `yes`, or **`known`**. `known` asserts only that the cloud can tell you
    the state, whichever state it is, and exists because asserting `no` grades the LAB'S MOOD
    rather than the learner. An exercise about *finding* hosts in RFM must not fail the moment
    there is one to find -- which is exactly what happened when a Windows update walked
    FALCON-LAB-WIN into RFM. Whether the fleet is healthy is a judgement for an `attest`, where
    a human belongs; whether the platform-agnostic read WORKS is the automatable part.
    """
    if expect not in ("no", "yes", "known"):
        return {"ok": None, "reason": f"rfm_state expect must be no|yes|known, got {expect!r}"}
    clients = _clients()
    if not clients:
        return {"ok": None, "reason": _why_unavailable()}
    try:
        r = clients["hosts"].query_devices_by_filter(
            filter=f"hostname:*'*{hostname}*'", limit=500)
        if r.get("status_code") != 200:
            return {"ok": None, "reason": f"devices read failed (HTTP {r.get('status_code')}) -- "
                    f"check the key's Hosts read scope"}
        ids = ((r.get("body") or {}).get("resources")) or []
        if not ids:
            return {"ok": None, "reason": f"no visible host matching {hostname!r}"}
        d = clients["hosts"].get_device_details(ids=ids)
        devices = ((d.get("body") or {}).get("resources")) or []
        # Match client-side: FQL's wildcard is broad, and hostname case differs by platform
        # (Windows registers FALCON-LAB-WIN, Linux falcon-lab-lnx).
        want = hostname.lower()
        # Keyed by device id, not hostname: this CID has carried a stale duplicate FALCON-LAB-WIN
        # from a rollback+reinstall, and keying by name would silently collapse two AIDs into one
        # verdict -- reporting the healthy twin and hiding the one in RFM.
        states = [(dev.get("hostname") or "?", (dev.get("device_id") or "")[:8],
                   dev.get("reduced_functionality_mode") or "unknown")
                  for dev in devices if want in (dev.get("hostname") or "").lower()]
        if not states:
            return {"ok": None, "reason": f"no visible host matching {hostname!r}"}
        multi = len(states) > 1
        shown = ", ".join(f"{h}{'/' + aid if multi else ''} rfm={s}"
                          for h, aid, s in sorted(states))
        unknown = [h for h, _, s in states if s == "unknown"]
        if expect == "known":
            if unknown:
                return {"ok": None, "reason": f"cloud does not report RFM state for "
                                              f"{', '.join(sorted(set(unknown)))} ({shown})"}
            return {"ok": True, "reason": f"{shown} -- state readable from the cloud"}
        if len(unknown) == len(states):
            return {"ok": None, "reason": f"cloud reports RFM state unknown ({shown})"}
        ok = all(s == expect for _, _, s in states if s != "unknown")
        return {"ok": ok, "reason": f"{shown} (expected {expect})"}
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "reason": f"Falcon API read failed: {str(e)[:120]}"}


def host_group_exists(name):
    """Does a host group named `name` exist? (Host Groups read scope.)

    The console check this replaces asserted only that the string "Falcon Lab" appeared somewhere
    on the host-groups page -- which a *deleted* group still satisfies if its name lingers in a
    filter chip or a recent-activity row. Resolving the name to an ID is the real question.
    """
    clients = _clients()
    if not clients:
        return {"ok": None, "reason": _why_unavailable()}
    try:
        gid = _resolve_group_id(clients, name)
        if gid is None:
            return {"ok": False, "reason": f"no host group named {name!r} exists"}
        return {"ok": True, "reason": f"host group {name!r} exists (id {gid[:8]}...)"}
    except ConsoleUnavailable as e:
        return {"ok": None, "reason": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "reason": f"Falcon API read failed: {str(e)[:120]}"}


def visible_hosts_matching(contains, at_least=1):
    """Are at least `at_least` VISIBLE hosts' names matching *contains*?

    Wildcard FQL rather than a client-side scan: a real CID may hold far more devices than the
    500-row page the group lookups get away with. Hidden hosts are excluded, so this reports what
    Host management would actually show.
    """
    clients = _clients()
    if not clients:
        return {"ok": None, "reason": _why_unavailable()}
    try:
        r = clients["hosts"].query_devices_by_filter(
            filter=f"hostname:*'*{contains}*'", limit=500)
        if r.get("status_code") != 200:
            return {"ok": None, "reason": f"devices read failed (HTTP {r.get('status_code')}) -- "
                    f"check the key's Hosts read scope"}
        n = len(((r.get("body") or {}).get("resources")) or [])
        return {"ok": n >= at_least,
                "reason": f"{n} visible host(s) matching {contains!r} (expected at least "
                          f"{at_least})"}
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "reason": f"Falcon API read failed: {str(e)[:120]}"}


def custom_ioc_exists(value_contains=None, ioc_type=None, action=None):
    """Is there a custom IOC, optionally matching value/type/action? (IOC Management read scope.)

    All three filters are optional and AND together, so the scenario decides how specific to be:
    "any IOC at all" is a weak but honest check, "this hash, blocking" is the real one.
    """
    clients = _clients()
    if not clients:
        return {"ok": None, "reason": _why_unavailable()}
    try:
        r = clients["ioc"].indicator_combined(limit=500)
        if r.get("status_code") != 200:
            return {"ok": None, "reason": f"IOC read failed (HTTP {r.get('status_code')}) -- "
                    f"check the key's IOC Management read scope"}
        rows = (r.get("body") or {}).get("resources") or []
        want = []
        if value_contains:
            want.append(f"value containing {value_contains!r}")
            rows = [i for i in rows
                    if value_contains.lower() in (i.get("value") or "").lower()]
        if ioc_type:
            want.append(f"type {ioc_type!r}")
            rows = [i for i in rows if (i.get("type") or "") == ioc_type]
        if action:
            want.append(f"action {action!r}")
            rows = [i for i in rows if (i.get("action") or "") == action]
        desc = " and ".join(want) if want else "any type"
        if rows:
            top = rows[0]
            return {"ok": True, "reason": f"{len(rows)} custom IOC(s) with {desc} "
                    f"(e.g. {top.get('type')} {(top.get('value') or '')[:24]} "
                    f"-> {top.get('action')})"}
        return {"ok": False, "reason": f"no custom IOC with {desc} exists"}
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "reason": f"Falcon API read failed: {str(e)[:120]}"}


# --- WRITE side. Everything above this line only reads. ------------------------------------
#
# The key was read-only by choice until 2026-08-14, when IOC Management write was added -- and
# ONLY that. The reasoning is worth keeping, because the next "just add one more scope" will
# look equally harmless:
#
#   * IOCs are self-contained and trivially reversible. A wrong one blocks a hash; you delete it.
#     A wrong PREVENTION POLICY write can silently edit `platform_default` or a stock `Phase N`
#     template that every other exercise depends on, and nothing would announce it.
#   * The lab must never perform the action an objective asks the LEARNER to perform. Creating an
#     IOC is the whole point of `ioc-blocklist-hash`, so the lab must not create one there. These
#     helpers exist for TEARDOWN, for prerequisites that are not the lesson, and for staging
#     deliberately-broken states -- never for doing the exercise.
#   * Anything the lab creates, the lab must not count as evidence. `custom_ioc_exists()` is the
#     grader; if a scenario's setup created the thing it grades, the check tests the grader.
#
# The guardrail is a TAG, because IOCs have no name to prefix. Every lab-created IOC carries
# LAB_TAG, and delete only ever targets that tag -- so a user-authored IOC cannot be removed by
# this code even if the filter is wrong.
LAB_TAG = "falcon-lab-managed"

# Objects the lab must never modify, whatever a caller asks for. Nothing here is IOC-specific
# yet; it exists so the list has an obvious home when scopes widen.
NEVER_TOUCH = ("platform_default", "Phase 1 - initial deployment",
               "Phase 2 - interim protection", "Phase 3 - optimal protection")

VALID_IOC_ACTIONS = ("no_action", "allow", "detect", "prevent", "prevent_no_ui")


def ioc_create(value, ioc_type="sha256", action="prevent", description="",
               host_groups=None, platforms=("windows",), expiration_days=7):
    """Create a lab-owned custom IOC. Needs IOC Management **write**.

    Tagged LAB_TAG so `ioc_clean()` can find it and nothing else can. Expires by default: a lab
    that forgets to clean up should decay to safe on its own rather than leave a blocking IOC in
    the CID indefinitely.
    """
    clients = _clients()
    if not clients:
        return {"ok": None, "reason": _why_unavailable()}
    if action not in VALID_IOC_ACTIONS:
        return {"ok": None, "reason": f"action must be one of {VALID_IOC_ACTIONS}"}
    import datetime as _dt
    body = {"type": ioc_type, "value": value, "action": action,
            "severity": "medium", "platforms": list(platforms),
            "description": description or "created by the Falcon lab",
            "tags": [LAB_TAG], "applied_globally": not host_groups}
    if host_groups:
        try:
            ids = [_resolve_group_id(clients, g) for g in host_groups]
        except ConsoleUnavailable as e:
            return {"ok": None, "reason": str(e)}
        if any(i is None for i in ids):
            return {"ok": False, "reason": f"unknown host group in {host_groups!r}"}
        body["host_groups"] = ids
    if expiration_days:
        body["expiration"] = ((_dt.datetime.now(_dt.timezone.utc)
                               + _dt.timedelta(days=expiration_days))
                              .strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    try:
        r = clients["ioc"].indicator_create(indicators=[body], comment="falcon lab")
        code = r.get("status_code")
        if code in (403, 401):
            return {"ok": None, "reason": "IOC create refused (HTTP %s) -- the key needs IOC "
                    "Management **write**, not just read" % code}
        if code not in (200, 201):
            errs = "; ".join(str(e.get("message")) for e in (r.get("body") or {}).get("errors")
                             or [])[:160]
            return {"ok": False, "reason": f"IOC create failed (HTTP {code}) {errs}"}
        res = ((r.get("body") or {}).get("resources") or [{}])[0]
        return {"ok": True, "id": res.get("id"),
                "reason": f"created {ioc_type} IOC {value[:16]}... action={action}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "reason": f"IOC create failed: {str(e)[:140]}"}


def ioc_clean(dry_run=False):
    """Delete every IOC tagged LAB_TAG. Never touches anything else -- that is the whole point.

    Filters CLIENT-SIDE on the tag rather than trusting an FQL `tags:` filter: the same
    server-side matching that returned nothing for a host group that plainly exists would here
    mean deleting the wrong set, and a filter bug that under-matches is an annoyance while one
    that over-matches destroys a user's IOCs.
    """
    clients = _clients()
    if not clients:
        return {"ok": None, "reason": _why_unavailable()}
    try:
        r = clients["ioc"].indicator_combined(limit=500)
        if r.get("status_code") != 200:
            return {"ok": None, "reason": f"IOC read failed (HTTP {r.get('status_code')})"}
        mine = [i for i in ((r.get("body") or {}).get("resources") or [])
                if LAB_TAG in (i.get("tags") or [])]
        if not mine:
            return {"ok": True, "removed": 0, "reason": f"no IOCs tagged {LAB_TAG}"}
        vals = ", ".join((i.get("value") or "")[:12] for i in mine[:4])
        if dry_run:
            return {"ok": True, "removed": 0,
                    "reason": f"would delete {len(mine)} lab IOC(s): {vals}"}
        d = clients["ioc"].indicator_delete(ids=[i["id"] for i in mine],
                                            comment="falcon lab teardown")
        code = d.get("status_code")
        if code in (403, 401):
            return {"ok": None, "reason": f"IOC delete refused (HTTP {code}) -- key needs IOC "
                                          f"Management write"}
        if code != 200:
            return {"ok": False, "reason": f"IOC delete failed (HTTP {code})"}
        return {"ok": True, "removed": len(mine),
                "reason": f"deleted {len(mine)} lab IOC(s): {vals}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "reason": f"IOC delete failed: {str(e)[:140]}"}


def ioa_rule_group_enabled(name):
    """Is there a custom IOA rule group named `name` that is enabled? (Custom IOAs read scope.)

    Matches client-side, like the host-group lookup, to avoid FQL name-filter quirks.
    """
    clients = _clients()
    if not clients:
        return {"ok": None, "reason": _why_unavailable()}
    try:
        r = clients["custom_ioa"].query_rule_groups_full(limit=500)
        if r.get("status_code") != 200:
            return {"ok": None, "reason": f"custom-IOA read failed (HTTP {r.get('status_code')})"
                    f" -- check the key's Custom IOAs read scope"}
        for g in (r.get("body") or {}).get("resources") or []:
            if (g.get("name") or "") == name:
                if g.get("enabled"):
                    rules = g.get("rules") or []
                    on = [x for x in rules if x.get("enabled")]
                    return {"ok": True, "reason": f"IOA rule group {name!r} is enabled "
                            f"with {len(on)}/{len(rules)} rule(s) enabled"}
                return {"ok": False, "reason": f"IOA rule group {name!r} exists but is DISABLED"}
        return {"ok": False, "reason": f"no custom IOA rule group named {name!r} exists"}
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "reason": f"Falcon API read failed: {str(e)[:120]}"}


def recent_detection_contains(token, within_min=30):
    """Did any alert in the last `within_min` minutes contain `token` anywhere in its payload?

    The trigger uses a distinctive token in the command line, so a substring match over the
    alert JSON is a reliable, rule-name-independent way to confirm the IOA actually fired.
    Needs the Alerts read scope. Detection latency is real -- this may read False for a minute
    or two after the trigger, so re-grade.
    """
    import datetime as _dt
    import json as _json
    clients = _clients()
    if not clients:
        return {"ok": None, "reason": _why_unavailable()}
    try:
        since = (_dt.datetime.now(_dt.timezone.utc)
                 - _dt.timedelta(minutes=within_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
        q = clients["alerts"].query_alerts_v2(
            filter=f"created_timestamp:>'{since}'", limit=200, sort="created_timestamp|desc")
        if q.get("status_code") != 200:
            return {"ok": None, "reason": f"alerts read failed (HTTP {q.get('status_code')}) -- "
                    f"check the key's Alerts read scope"}
        ids = (q.get("body") or {}).get("resources") or []
        if not ids:
            return {"ok": False, "reason": f"no alerts at all in the last {within_min} min "
                    f"(trigger the IOA, then allow a minute for the detection to surface)"}
        d = clients["alerts"].get_alerts_v2(composite_ids=ids)
        for a in (d.get("body") or {}).get("resources") or []:
            if token in _json.dumps(a):
                return {"ok": True, "reason": f"a detection containing {token!r} fired "
                        f"(the IOA matched)"}
        return {"ok": False, "reason": f"{len(ids)} recent alert(s), but none contain {token!r} "
                f"-- has the rule matched and the detection surfaced yet?"}
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "reason": f"Falcon API read failed: {str(e)[:120]}"}


def ml_exclusion_exists(path_contains, group_name=None):
    """Is there an ML exclusion whose path contains `path_contains`, applied to `group_name`
    (or globally)? (ML Exclusions read scope; Host Groups read to resolve the group name.)

    Substring-matches the exclusion's `value` so it survives the console's path/glob formatting.
    """
    clients = _clients()
    if not clients:
        return {"ok": None, "reason": _why_unavailable()}
    try:
        q = clients["ml_exclusions"].query_exclusions(limit=500)
        if q.get("status_code") != 200:
            return {"ok": None, "reason": f"ML-exclusions read failed (HTTP {q.get('status_code')})"
                    f" -- check the key's Machine Learning Exclusions read scope"}
        ids = (q.get("body") or {}).get("resources") or []
        if not ids:
            return {"ok": False, "reason": "no ML exclusions exist in this CID"}
        d = clients["ml_exclusions"].get_exclusions(ids=ids)
        gid = _resolve_group_id(clients, group_name) if group_name else None
        path_hit = False
        for e in (d.get("body") or {}).get("resources") or []:
            val = e.get("value") or ""
            if path_contains.lower() not in val.lower():
                continue
            path_hit = True
            if not group_name:
                return {"ok": True, "reason": f"ML exclusion for {val!r} exists"}
            if e.get("applied_globally"):
                return {"ok": True, "reason": f"ML exclusion for {val!r} applies globally"}
            if gid and gid in _group_ids(e.get("groups")):
                return {"ok": True, "reason": f"ML exclusion for {val!r} assigned to {group_name!r}"}
        if path_hit:
            return {"ok": False, "reason": f"an ML exclusion matching {path_contains!r} exists but "
                    f"is not applied to {group_name!r} (nor globally)"}
        return {"ok": False, "reason": f"no ML exclusion whose path contains {path_contains!r}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "reason": f"Falcon API read failed: {str(e)[:120]}"}


def host_contained(hostname, expect=True):
    """Is `hostname` under NETWORK containment? (Hosts read scope.)

    Reads the device `status` field, whose values are `normal` / `contained`, plus the two
    transitional ones the console shows as "Containment Pending" and "Lift Containment Pending".
    A pending state is reported as `None` -- in flight is neither pass nor fail, and calling it
    either would make the check a coin toss during the window it is most likely to be run.

    NOT the same thing as `filesystem_containment_status`, which is the prevention policy's
    File System Containment setting. Different feature, different field, similar name.
    """
    clients = _clients()
    if not clients:
        return {"ok": None, "reason": _why_unavailable()}
    try:
        q = clients["hosts"].query_devices_by_filter(filter=f"hostname:'{hostname}'", limit=20)
        ids = (q.get("body") or {}).get("resources") or []
        if not ids:
            return {"ok": False, "reason": f"no visible host named {hostname!r}"}
        d = clients["hosts"].get_device_details(ids=ids)
        for dev in (d.get("body") or {}).get("resources") or []:
            st = (dev.get("status") or "").lower()
            if "pending" in st:
                return {"ok": None, "reason": f"{hostname} is {st!r} -- containment is in "
                        f"flight; re-grade in a moment"}
            contained = st == "contained"
            if contained == bool(expect):
                return {"ok": True, "reason": f"{hostname} status={st!r} (expected "
                        f"{'contained' if expect else 'not contained'})"}
            return {"ok": False, "reason": f"{hostname} status={st!r}, expected "
                    f"{'contained' if expect else 'normal'}"}
        return {"ok": None, "reason": f"no device detail returned for {hostname!r}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "reason": f"Falcon API read failed: {str(e)[:120]}"}


def quarantined_file(hostname, name_contains=None, within_min=None):
    """Is there a quarantined file on `hostname` (optionally whose record contains
    `name_contains`)? (Quarantine read scope.)

    `within_min` bounds it to records created recently, and scenarios that stage their own
    trigger should always set it. **A quarantine record OUTLIVES the file and the IOC that
    caused it**: after the blocklist entry is removed the host store empties, but the cloud
    record persists with `state: quarantined`. Without a time bound this check passes on a
    previous run's artifact while the learner has done nothing -- the same false pass as
    grading "the file is gone from disk", which measured Windows Defender.
    """
    import datetime as _dt
    import json as _json
    clients = _clients()
    if not clients:
        return {"ok": None, "reason": _why_unavailable()}
    try:
        q = clients["quarantine"].query_quarantine_files(
            filter=f"hostname:'{hostname}'", limit=200)
        if q.get("status_code") != 200:
            return {"ok": None, "reason": f"quarantine read failed (HTTP {q.get('status_code')}) "
                    f"-- check the key's Quarantined Files read scope"}
        ids = (q.get("body") or {}).get("resources") or []
        if not ids:
            return {"ok": False, "reason": f"no quarantined files on {hostname} "
                    f"(is the prevention policy's Quarantine setting on and applied?)"}
        d = clients["quarantine"].get_quarantine_files(ids=ids)
        rows = (d.get("body") or {}).get("resources") or []
        window = ""
        if within_min:
            cutoff = (_dt.datetime.now(_dt.timezone.utc)
                      - _dt.timedelta(minutes=within_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
            fresh = [f for f in rows if (f.get("date_created") or "") >= cutoff]
            window = f" in the last {within_min} min"
            if rows and not fresh:
                newest = max((f.get("date_created") or "") for f in rows)
                return {"ok": False, "reason": f"{len(rows)} quarantined file(s) on {hostname} "
                        f"but none{window} (newest {newest}) -- that is a PREVIOUS run's record; "
                        f"a quarantine record outlives the file and the IOC that caused it"}
            rows = fresh
        if not name_contains:
            return {"ok": bool(rows),
                    "reason": (f"{len(rows)} quarantined file(s) on {hostname}{window}" if rows
                               else f"no quarantined files on {hostname}{window}")}
        for f in rows:
            if name_contains.lower() in _json.dumps(f).lower():
                return {"ok": True, "reason": f"a quarantined file matching {name_contains!r} on "
                        f"{hostname}{window} (state: {f.get('state') or 'quarantined'}, "
                        f"created {f.get('date_created')})"}
        return {"ok": False, "reason": f"{len(rows)} quarantined file(s) on {hostname}{window}, "
                f"but none match {name_contains!r} -- has the binary been RUN yet? Writing it to "
                f"disk does not quarantine it; only attempting to execute it does"}
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "reason": f"Falcon API read failed: {str(e)[:120]}"}
