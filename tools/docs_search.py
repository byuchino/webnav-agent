#!/usr/bin/env python3
"""Search the harvested CrowdStrike documentation.

  ./tools/docs_search.py "how long are inactive hosts kept"
  ./tools/docs_search.py -k 5 "reduced functionality mode causes"
  ./tools/docs_search.py --files "sensor update policy"

## Why BM25 and not embeddings

The obvious upgrade here is a vector database, and for this corpus it is the wrong one.

- **Size.** 378 files, ~3 MB. Scanning it costs milliseconds; there is no latency problem to
  solve, and an embedding round trip would *add* one.
- **Vocabulary.** Product documentation is dense with exact strings — `falconctl`,
  `AutoHidePeriod`, `RFM`, `0xc0000225`, `--rfm-state`. Lexical search is not merely adequate
  for those, it is better: embeddings blur precisely the tokens that identify the answer.
  Every lookup this project has needed so far was an exact-term lookup.
- **Measured, not assumed.** WEB_NAVIGATION_TECHNIQUES.md §9 tried this and concluded:
  "DON'T use vector embeddings for in-page search. It is one document; BM25 wins on both
  quality and zero dependencies." Its episodic memory (§11) also found retrieval quality rode
  on the metadata filter rather than the embedding.
- **Cost.** Tokens are spent on what comes BACK, and a ranked passage is smaller than a broad
  grep with context. That saving comes from ranking and capping, which BM25 gives for free —
  not from how the ranking was computed.

BM25 earns its place over plain grep by RANKING and CAPPING — fewer tokens come back than
from a broad grep with context, and the best passage is first. That is the whole of its
advantage, and it is worth having.

It does **not** bridge a vocabulary gap, and it is worth being precise about that because the
temptation is to claim otherwise. Tested here: "how do I stop old hosts piling up in the
console" returned *disable detections on a host* and *reset an API client secret* — it matched
on "old" and missed host retention entirely. BM25 is lexical; a semantic query defeats it just
as it defeats grep.

An embedding model IS available locally (LM Studio serves nomic-embed-text alongside the
chat model), so adding a semantic stage would cost no new infrastructure. It is not done here
because every documentation lookup this project has actually needed was terminological —
"checksum", "hide host", "reduced functionality mode", "host retention" — and lexical search
is better than embeddings for exact product strings, not merely adequate. Add the semantic
stage when a real conceptual query fails, not in anticipation of one.
"""
import argparse
import math
import os
import pathlib
import re
import sys

DOCS = pathlib.Path(os.environ.get("DOCS_OUT", pathlib.Path.home() / "falcon-docs"))

_STOP = set("""a an the of to in is are was were be been being on for and or with by as at from
that this it its which who whom what when where how why into about above below over under do
does did has have had not no nor than then they their them you your our we i he she his her
can could would should will may might must each any all some most if you use used using can""".split())


def terms(q):
    return [w for w in re.findall(r"[a-z0-9_.-]+", (q or "").lower())
            if w not in _STOP and len(w) > 1]


def chunks_of(text, size=900):
    """Paragraph-packed chunks. Splitting mid-sentence destroys the passage."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 1 <= size:
            cur = (cur + "\n" + p).strip()
        else:
            if cur:
                out.append(cur)
            cur = p if len(p) <= size else ""
            if len(p) > size:
                for i in range(0, len(p), size):
                    out.append(p[i:i + size])
    if cur:
        out.append(cur)
    return out


def load():
    docs = []
    for f in sorted(DOCS.rglob("*.txt")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        title = text.split("\n", 1)[0].lstrip("# ").strip()
        for c in chunks_of(text):
            docs.append({"file": str(f.relative_to(DOCS)), "title": title[:70], "text": c})
    return docs


def search(docs, query, k=3):
    """Standard BM25. idf drives down terms that appear in every chunk; length
    normalisation stops a long keyword-stuffed passage beating a short precise one."""
    q = terms(query)
    if not q or not docs:
        return []
    toks = [re.findall(r"[a-z0-9_.-]+", d["text"].lower()) for d in docs]
    dls = [len(t) for t in toks]
    n = len(docs)
    avgdl = (sum(dls) / n) or 1.0
    df = {t: sum(1 for tk in toks if t in tk) for t in set(q)}
    idf = {t: max(0.0, math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))) for t in df}
    k1, b = 1.5, 0.75

    scored = []
    for i, tk in enumerate(toks):
        s = 0.0
        for t in q:
            f = tk.count(t)
            if f:
                s += idf[t] * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dls[i] / avgdl))
        if s > 0:
            scored.append((s, i))
    scored.sort(reverse=True)
    return [{"score": round(s, 2), **docs[i]} for s, i in scored[:k]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="+")
    ap.add_argument("-k", type=int, default=3, help="passages to return")
    ap.add_argument("--files", action="store_true", help="filenames and scores only")
    a = ap.parse_args()

    docs = load()
    if not docs:
        sys.exit(f"no documentation in {DOCS} — run ./tools/harvest_all.sh")
    hits = search(docs, " ".join(a.query), a.k)
    if not hits:
        print("no match")
        return 0
    for h in hits:
        print(f"\n=== {h['score']:>6}  {h['file']}")
        if not a.files:
            print(h["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
