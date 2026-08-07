"""Human-in-the-loop for irreversible actions (guide §17 mitigation 6).

The last of the guide's §17 mitigations and the one the reference implementation never
built. It becomes load-bearing the moment the browser profile carries a real session: on an
anonymous profile the worst case of a mis-grounded click is a wasted step; on an
authenticated one it is a deleted record, a sent message, or a placed order.

Two things this is NOT:

1. **Not a classifier you should trust.** The name match is a tripwire, not a proof.
   "Continue" can be the final step of a purchase and "Delete" can appear in a help article.
   Anything that genuinely must not happen without a human belongs under WRITES or READONLY,
   where the question is asked on every mutation regardless of wording.
2. **Not a substitute for the allowlist.** That stops the agent reaching a hostile origin.
   This stops it doing something irreversible on an origin it is allowed to reach — including
   one where an injection is hosted on the allowed site itself, which the allowlist cannot
   see. They cover different halves of the same threat.

It FAILS CLOSED. With no interactive terminal there is no human to ask, so the answer is no.
"""
import asyncio
import logging
import re
import sys

log = logging.getLogger("agent.consent")

AUTO = "auto"                # never ask — anonymous fixtures and read-only scraping
DESTRUCTIVE = "destructive"  # ask when the target's wording looks irreversible (default)
WRITES = "writes"            # ask before every mutation, whatever it is called
READONLY = "readonly"        # refuse every mutation; links may still be followed

POLICIES = (AUTO, DESTRUCTIVE, WRITES, READONLY)

# Ops that can change state on the far side. `navigate`/`scroll`/`wait_for_text` and the
# read-only extractors are not here — following a link is not a mutation.
MUTATING = {"click", "check", "setval", "submit"}

# Deliberately not exhaustive, and deliberately not too broad: a gate that fires on
# everything trains the operator to approve without reading, which is worse than no gate.
_DESTRUCTIVE = re.compile(
    r"""
      \b(delete|destroy|erase|wipe|shred|permanently)\b
    | \bremove\b
    | \b(send|post|publish|submit\s+(order|application|report))\b
    | \b(buy|purchase|checkout|check\s*out|place\s+(the\s+)?order|pay|subscribe|upgrade|renew)\b
    | \b(transfer|withdraw|wire)\b
    | \b(deactivate|disable\s+account|close\s+account|unsubscribe)\b
    | \bcancel\s+(my\s+|the\s+)?(subscription|membership|plan|booking|reservation|order|account)\b
    | \b(sign|log)\s*out\b
    | \b(confirm|approve|authorize|authorise)\b
    | \breset\b
    """,
    re.I | re.X,
)


def classify(name, ctx=""):
    """Return the matched destructive phrase, or None. Row context is checked too — a
    'Remove' button is judged partly by the row it sits in."""
    for hay in (name or "", ctx or ""):
        m = _DESTRUCTIVE.search(hay)
        if m:
            return m.group(0).strip()
    return None


def _describe(op, index, name, role, ctx, text):
    bits = [op]
    if index is not None:
        bits.append(f"[{index}]")
    if role:
        bits.append(role)
    if name:
        bits.append(f'"{name}"')
    if text:
        bits.append(f"<- {text!r}")
    line = " ".join(bits)
    return line + (f"\n  context : (in: {ctx})" if ctx else "")


async def _ask(prompt_block):
    """Ask a human. No TTY means no human, which means no."""
    if not (sys.stdin and sys.stdin.isatty()):
        return False, "no interactive terminal — failing closed"
    sys.stderr.write(prompt_block)
    sys.stderr.flush()
    try:
        answer = await asyncio.to_thread(input, "  approve? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False, "operator declined (interrupted)"
    ok = answer.strip().lower() in ("y", "yes")
    return ok, "operator approved" if ok else "operator declined"


async def gate(policy, op, *, index=None, name="", role="", ctx="", text="",
               url="", goal=""):
    """Decide whether one action may proceed. Returns (allowed, reason).

    The reason is surfaced to the model as a TOOL RESULT, exactly like a blocked
    navigation, so it can choose a different route instead of retrying blindly.
    """
    if policy == AUTO or op not in MUTATING:
        return True, ""

    if policy == READONLY:
        # Following a link changes what we are looking at, not what the site holds.
        if op == "click" and role == "link":
            return True, ""
        return False, f"policy is readonly — '{op}' refused without asking"

    hit = classify(name, ctx)
    if policy == DESTRUCTIVE and not hit:
        return True, ""

    reason = (f"wording matches irreversible intent: {hit!r}" if hit
              else f"policy is '{policy}' — every mutation is confirmed")

    block = (
        "\n━━ CONFIRM ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  goal    : {goal[:150]}\n"
        f"  action  : {_describe(op, index, name, role, ctx, text)}\n"
        f"  page    : {url[:110]}\n"
        f"  why ask : {reason}\n"
    )
    allowed, why = await _ask(block)
    log.warning("consent %s: %s (%s)", "GRANTED" if allowed else "DENIED",
                _describe(op, index, name, role, "", text).replace("\n", " "), why)
    return allowed, why
