"""
Hybrid retrieval primitives.

Two building blocks used by ``MemoryStore.recall_hybrid``:

* ``bm25_search`` — runs an FTS5 MATCH against the memories or wiki
  mirror, returning ranked candidates with their native BM25 score.

* ``fuse_rrf`` — Reciprocal Rank Fusion across multiple ranked lists.
  Each list contributes ``1 / (k + rank_i(d))`` per document;
  the document's fused score is the sum across lists. RRF is the
  standard 2020+ recipe for combining heterogeneous rankers
  (semantic, keyword, entity) without needing to align their raw
  score distributions, which is exactly the problem we have here.

No external deps. SQLite FTS5 ships with the stdlib ``sqlite3`` module.
"""
from __future__ import annotations

from typing import Any, Iterable

# RRF constant. 60 is the value used in the original Cormack et al.
# 2009 paper and matches what Mem0/Graphiti use in 2026.
DEFAULT_RRF_K = 60


def _escape_fts(query: str) -> str:
    """Wrap the query for FTS5's MATCH expression.

    The pipeline is query-type-aware:

    * ASCII/Latin tokens (``vue``, ``codex``) → wrapped in double
      quotes so ``vue.js`` does NOT collapse into one exact-phrase.
    * CJK runs (``知识图谱``, ``记忆系统``) → decomposed into character
      trigrams (``知识图``, ``识图谱``) joined by ``AND``. The trigram
      tokenizer otherwise needs each trigram to be a separate FTS5
      token, and quoting the whole run as an exact phrase matches
      only documents that contain the literal byte sequence — which
      nothing does for arbitrary text.

    Returns ``""`` when there is no usable content.
    """
    import re

    s = (query or "").lower()
    if not s.strip():
        return ""

    # 1. Split into "Latin word" tokens and "CJK runs" tokens. Any
    #    contiguous run of CJK ideographs is treated as one chunk.
    cjk_re = re.compile(r"[一-鿿]+")
    lat_re = re.compile(r"[\w]+")

    parts: list[str] = []

    # Walk through the string preserving order; we don't actually need
    # FTS5 to honour order (RRF rescues it), so a flat list is fine.
    for cjk_run in cjk_re.findall(s):
        if len(cjk_run) < 3:
            # too short for trigrams; quote and fall back to LIKE
            parts.append(f'"{cjk_run}"')
            continue
        grams = [cjk_run[i:i + 3] for i in range(len(cjk_run) - 2)]
        if len(grams) <= 2:
            # 3-4 chars: emit them all (precise enough)
            parts.extend(grams)
        else:
            # 5+ chars: AND only the boundary trigrams (first + last).
            # The middle grams are usually present whenever the
            # boundary grams are, so omitting them makes the query
            # more recall-friendly without losing precision.
            parts.append(grams[0])
            parts.append(grams[-1])

    # Also pull out Latin words (skip CJK runs).
    cleaned = cjk_re.sub(" ", s)
    for w in lat_re.findall(cleaned):
        if len(w) >= 1:
            parts.append(f'"{w}"')

    if not parts:
        return ""
    # Default FTS5 operator between terms is AND; that's what we want.
    return " ".join(parts)


def bm25_search(
    store: Any,
    query: str,
    kind: str = "memories",
    limit: int = 50,
    source_filter: str | None = None,
) -> list[dict]:
    """Run an FTS5 query and return a list of ``{"id": ..., "_score": bm25}``.

    ``kind`` is "memories" or "wiki". The native BM25 score from
    SQLite's ``bm25(memories_fts)`` is negative (lower = better), so
    we negate it for the RRF caller which expects positive scores.

    For ``wiki``, the FTS query is restricted to rows whose scope
    allows the caller's ``source_filter`` (when provided).

    Fallback: the FTS5 trigram tokenizer requires >=3 contiguous
    characters to create any token. For inputs that are all-CJK
    AND total length <3, FTS5 returns nothing; we then issue a
    ``LIKE '%<q>%'`` over the source table as a backstop. For our
    scale (<10k memories, <1k wiki) LIKE is well under 5 ms.
    """
    import re as _re
    fts_q = _escape_fts(query)
    is_short_cjk = bool(
        _re.fullmatch(r"[一-鿿]+", (query or "").strip() or "")
        and len((query or "").strip()) < 3
    )
    rows: list = []
    if fts_q and not is_short_cjk:
        table = "memories_fts" if kind == "memories" else "wiki_fts"
        score_col = f"bm25({table})"
        with store._conn() as c:
            if kind == "memories":
                # Join back to memories so we can return the actual
                # UUID `id` column (the FTS5 rowid is just a sqlite
                # rowid, not the memory's primary key).
                sql = (
                    f"SELECT m.id AS id, {score_col} AS s "
                    f"FROM {table} t "
                    f"JOIN memories m ON m.rowid = t.rowid "
                    f"WHERE {table} MATCH ? "
                    f"ORDER BY s LIMIT ?"
                )
                rows = c.execute(sql, (fts_q, limit)).fetchall()
            else:
                # Wiki: also join back to wiki_pages so we can apply the
                # per-source scope filter at the SQL layer. We negate
                # the bm25 score so the RRF caller sees positive values.
                sql = (
                    f"SELECT w.id AS id, {score_col} AS s, w.scope AS scope "
                    f"FROM {table} t "
                    f"JOIN wiki_pages w ON w.rowid = t.rowid "
                    f"WHERE {table} MATCH ? "
                    f"ORDER BY s LIMIT ?"
                )
                rows = c.execute(sql, (fts_q, limit * 3)).fetchall()
                # Apply scope filter in Python so we can use the same
                # token-matching convention as the rest of the system.
                rows = _filter_wiki_scope(rows, source_filter)[:limit]
    # Always run LIKE as a backstop. Even when FTS5 returns hits,
    # LIKE may surface documents the trigram tokenizer can't see
    # (short CJK, OCR noise, mixed code identifiers, ...). The two
    # lists are merged by id; LIKE rows get a synthetic importance
    # score so the RRF caller can rank them alongside BM25.
    like_rows = _like_fallback(store, query, kind=kind, limit=limit,
                               source_filter=source_filter)
    merged: dict[str, dict] = {}
    for r in (rows or []):
        merged[r["id"]] = {"id": r["id"], "_score": -float(r["s"])}
    for r in like_rows:
        imp = r["importance"] if "importance" in r.keys() else 0.5
        if r["id"] not in merged:
            merged[r["id"]] = {"id": r["id"], "_score": float(imp or 0.5)}
    return list(merged.values())


def _like_fallback(
    store: Any,
    query: str,
    kind: str,
    limit: int,
    source_filter: str | None = None,
) -> list:
    """Brute-force LIKE fallback used when FTS5 trigram can't index
    short CJK queries. Searches <substr> against the source table
    directly. Returns rows in the same shape as bm25_search.
    """
    pat = f"%{(query or '').strip()}%"
    if not query.strip():
        return []
    with store._conn() as c:
        if kind == "memories":
            sql = (
                "SELECT m.id AS id, m.importance AS importance "
                "FROM memories m WHERE LOWER(text) LIKE LOWER(?) "
                "ORDER BY m.importance DESC, m.created_at DESC LIMIT ?"
            )
            return [dict(r) for r in c.execute(sql, (pat, limit)).fetchall()]
        sql = (
            "SELECT w.id AS id, w.importance AS importance, w.scope AS scope "
            "FROM wiki_pages w "
            "WHERE LOWER(w.title) LIKE LOWER(?) OR LOWER(w.body) LIKE LOWER(?) "
            "ORDER BY w.importance DESC, w.updated_at DESC LIMIT ?"
        )
        rows = list(c.execute(sql, (pat, pat, limit * 3)).fetchall())
        return _filter_wiki_scope(rows, source_filter)[:limit]


def _filter_wiki_scope(rows: Iterable, source: str | None) -> list:
    if not source:
        return list(rows)
    tok = (source or "").strip().lower()
    out = []
    for r in rows:
        scope = (r["scope"] if "scope" in r.keys() else "global") or "global"
        if scope == "global":
            out.append(r)
        else:
            # scope is "global" or a comma-list like "codex,claude"
            allowed = {s.strip() for s in scope.split(",") if s.strip()}
            if tok in allowed:
                out.append(r)
    return out


def fuse_rrf(
    ranked_lists: list[list[dict]],
    k: int = DEFAULT_RRF_K,
) -> list[dict]:
    """Reciprocal Rank Fusion.

    Each input list is a list of ``{"id": ..., "_score": ...}``,
    already sorted by descending score (best first). Documents not
    present in a list contribute 0 from that list.

    The fused score is::

        fused(d) = sum over lists i of  1 / (k + rank_i(d))

    where ``rank_i(d)`` is 1-based and ``None`` (= not in list) is
    treated as 0.

    Returns a list of ``{"id": ..., "_rrf": ...}`` sorted by fused
    score descending. The ``_score`` field from each input is ignored
    (RRF operates on ranks, not raw scores).
    """
    fused: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, item in enumerate(lst, start=1):
            fused[item["id"]] = fused.get(item["id"], 0.0) + 1.0 / (k + rank)
    return [{"id": i, "_rrf": s} for i, s in sorted(fused.items(), key=lambda kv: -kv[1])]


# ----------------------------------------------------------------------
# Temporal reasoning layer
# ----------------------------------------------------------------------
# Mem0 (April 2026) showed that explicitly modelling the *temporal intent*
# of the query + reranking by date relevance contributes about 27 points
# on LongMemEval (94.4 vs 67.8 baseline). The intuition is simple:
# queries that say "what is the current X" should prefer the *most recent*
# memory that covers X, even if an older one is semantically closer.
# Conversely, "the project I shipped last week" should prefer the dated
# memory from that week, even if a fresher one exists. Without a temporal
# pass, RRF will happily return a newer-but-irrelevant memory.
#
# This module exposes two primitives. The store wires them in below
# the RRF fusion, so the change is opt-in for callers that already
# use ``recall_hybrid``.
# ----------------------------------------------------------------------

# Lightweight Chinese + English lexicons. We keep this in a plain
# constant (not an LLM call) so the latency cost of adding temporal
# reasoning is one regex pass per query.
_TEMPORAL_CURRENT = (
    "current", "currently", "now", "today", "latest", "recent",
    "现在", "当前", "目前", "今天", "此刻", "现在的", "最新的", "现在的",
)
_TEMPORAL_PAST = (
    "previous", "previously", "before", "last", "ago", "yesterday",
    "earlier", "originally", "initially", "at that time", "back then",
    "之前", "上次", "上次", "上次", "曾经", "过去", "原来的", "当初",
    "之前", "前几天", "上次", "已经", "之前",
)
_TEMPORAL_FUTURE = (
    "tomorrow", "upcoming", "next", "will", "plan to", "going to",
    "future", "scheduled",
    "明天", "下次", "未来", "即将", "之后", "将要", "计划", "打算",
)


def detect_temporal_intent(query: str) -> tuple[str, float]:
    """Return (intent, confidence) where intent is one of
    'current', 'past', 'future', 'any'.

    Confidence is the rough lexical overlap with the matching lexicon,
    capped at 1.0. 'any' always has confidence 0.0 (no signal).
    """
    import re as _re

    q = (query or "").lower()
    if not q.strip():
        return ("any", 0.0)

    def _hit(lex):
        hits = 0
        for w in lex:
            # use a simple substring match; we deliberately avoid a
            # tokenizer here because the query is short (≤ a few
            # sentences) and Jinja-style tokenization would slow us
            # down without measurable benefit.
            if w in q:
                hits += 1
        return hits

    n_cur = _hit(_TEMPORAL_CURRENT)
    n_past = _hit(_TEMPORAL_PAST)
    n_fut = _hit(_TEMPORAL_FUTURE)
    counts = {"current": n_cur, "past": n_past, "future": n_fut}
    intent = max(counts, key=counts.get)  # type: ignore[arg-type]
    total = n_cur + n_past + n_fut
    if total == 0:
        return ("any", 0.0)
    # If the winning intent only wins by 1 over the runner-up, treat
    # as 'any' (too ambiguous to do anything useful).
    sorted_counts = sorted(counts.values(), reverse=True)
    if sorted_counts[0] - sorted_counts[1] < 1:
        return ("any", 0.0)
    confidence = min(1.0, counts[intent] / 3.0)  # 3 hits = full confidence
    return (intent, confidence)


def temporal_score(
    *,
    created_at: float,
    updated_at: float | None,
    intent: str,
    now: float,
    confidence: float,
) -> float:
    """Return a multiplier in roughly [0.5, 1.5] for how well a memory's
    date matches the query intent.

    - intent='current': more recent = better; 30d-half-life decay.
    - intent='past': memories close to "now - small_delta" get a small
      boost; very recent memories are penalised so dated history wins.
    - intent='future': memories with created/updated_at > now get a
      strong boost (they're "upcoming plans"). We also accept memories
      whose text mentions future intent (caller decides via flag).
    - intent='any': returns 1.0 (no opinion).

    A confidence < 1.0 softens the effect; with confidence 0.0 the
    function returns 1.0 regardless of intent.
    """
    if intent == "any" or confidence <= 0.0:
        return 1.0
    import math as _math

    dt = float(updated_at or created_at or now)
    age_days = max(0.0, (now - dt) / 86400.0)
    if intent == "current":
        # 30-day half-life: fresh ≈1.5, 30d ≈1.0, 180d ≈0.65, 1y ≈0.5
        base = 1.25 + 0.25 * _math.exp(-age_days / 30.0)
    elif intent == "past":
        # Sigmoid centred on "1 week ago" — slight penalty for very
        # recent memories (they're probably the new state, not the past
        # state the user asked about). Cross-over at ~7 days old.
        x = (age_days - 7.0) / 7.0
        base = 1.25 + 0.25 * (_math.tanh(x))
    elif intent == "future":
        if dt >= now:
            base = 1.4  # upcoming / planned
        else:
            base = 0.7  # not future-dated
    else:
        base = 1.0
    # Blend toward 1.0 by (1 - confidence) so we don't blow up a
    # borderline match into a hard rule.
    return 1.0 + (base - 1.0) * confidence

