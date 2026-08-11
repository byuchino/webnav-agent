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
                              Quarantine)
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


def quarantined_file(hostname, name_contains=None):
    """Is there a quarantined file on `hostname` (optionally whose record contains
    `name_contains`, e.g. 'eicar')? (Quarantine read scope.)

    A real functional check: EICAR is genuinely quarantined when the prevention policy's
    Quarantine setting is on, so this proves the file reached the quarantine store.
    """
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
        if not name_contains:
            return {"ok": True, "reason": f"{len(ids)} quarantined file(s) on {hostname}"}
        d = clients["quarantine"].get_quarantine_files(ids=ids)
        for f in (d.get("body") or {}).get("resources") or []:
            if name_contains.lower() in _json.dumps(f).lower():
                return {"ok": True, "reason": f"a quarantined file matching {name_contains!r} on "
                        f"{hostname} (state: {f.get('state') or 'quarantined'})"}
        return {"ok": False, "reason": f"{len(ids)} quarantined file(s) on {hostname}, but none "
                f"match {name_contains!r} -- has EICAR been dropped and quarantined yet?"}
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "reason": f"Falcon API read failed: {str(e)[:120]}"}
