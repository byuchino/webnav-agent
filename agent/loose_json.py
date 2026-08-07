"""Forgiving JSON parsing and repair (guide §14.3).

The guide measured this as worth ~+1 (noise) because llama.cpp's grammar-constrained
decoding already guaranteed syntax. On LM Studio it is NOT redundant: `response_format`
json_schema mode is clean, but any unconstrained call (the plan step, the repair retry)
comes back wrapped in ```json fences. Keep it.
"""
import ast
import json
import re

_FENCE = re.compile(r"```(?:json|js|javascript|python)?\s*|```", re.I)
_TRUE = re.compile(r"\bTrue\b")
_FALSE = re.compile(r"\bFalse\b")
_NULLY = re.compile(r"\b(None|undefined|NaN)\b")
_BARE_KEY = re.compile(r"([{,]\s*)([A-Za-z_$][\w$]*)(\s*:)")
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def extract_object(text):
    """Outermost balanced {...}, IGNORING braces inside string literals, with escape handling.

    A naive text.find('{') / text.rfind('}') breaks on any JSON value containing a brace —
    that exact bug was part of the guide's 7/16 -> 16/16 network-suite fix.
    """
    if not text:
        return None
    s = text.find("{")
    if s < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    quote = ""
    for i in range(s, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
            continue
        if ch in "\"'":
            in_str = True
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[s:i + 1]
    return None


def _repairs(s):
    s = _FENCE.sub("", s)
    s = _TRUE.sub("true", s)
    s = _FALSE.sub("false", s)
    s = _NULLY.sub("null", s)
    s = _BARE_KEY.sub(r'\1"\2"\3', s)      # {op: "click"} -> {"op": "click"}
    s = _TRAILING_COMMA.sub(r"\1", s)
    return s


def _attempts(cand):
    # ast.literal_eval at tier 2 safely handles Python-flavoured output (True/None, single
    # quotes, trailing commas) with no exec.
    for fn in (json.loads, ast.literal_eval, lambda c: json.loads(_repairs(c))):
        try:
            v = fn(cand)
            if isinstance(v, dict):
                return v
        except Exception:
            pass
    if '"' not in cand:                     # last resort: no double quotes at all
        try:
            v = json.loads(_repairs(cand.replace("'", '"')))
            if isinstance(v, dict):
                return v
        except Exception:
            pass
    return None


def parse(text):
    raw = (text or "").strip()
    seen = []
    for cand in (raw, _FENCE.sub("", raw).strip(), extract_object(raw)):
        if not cand or cand in seen:
            continue
        seen.append(cand)
        v = _attempts(cand)
        if v is not None:
            return v
    return None
