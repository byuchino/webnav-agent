"""A local-LLM web-navigation agent over CDP, built from WEB_NAVIGATION_TECHNIQUES.md.

Phase 1 (the load-bearing core, ~80% of the value) plus the three highest-value macros:

  cdp.py         Layer 0 — transport, exception surfacing, trusted input
  snapshot.py    Layers 1-3 — v4 observation, settle, act-by-index with staleness
  skills.py      Layers 4-5 — intent macros, guarded eval_js, navigation allowlist
  llm.py         §14 — the model I/O contract (LM Studio json_schema variant)
  loose_json.py  §14.3 — forgiving parse and repair
  agent.py       §15.1 — the observe/decide/act/report loop and op dispatcher
"""
