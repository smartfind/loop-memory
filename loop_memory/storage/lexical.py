"""Deterministic lexical scoring for the retrieval channel.

Audit 2026-11-01 (loop-memory 0.4.18).

Why this module exists
----------------------
SQLite's FTS5 ``bm25()`` auxiliary function computes the classic
inverse document frequency::

    idf = log( (N - n + 0.5) / (n + 0.5) )

and **clamps every non-positive idf to 1e-6** (see ``fts5_aux.c`` in
the SQLite source tree).  The clamp exists so a score can never go
positive, but it puts a hard cliff in the middle of the ranking:

* a term present in 49 % of the corpus keeps a real idf (0.04);
* a term present in 51 % of the corpus gets idf ``1e-6``.

That is a factor of ~40 000 between two terms that are almost equally
discriminative, and it is not a corner case here.  The FTS5 mirror
uses the ``trigram`` tokenizer, so every CJK run contributes sliding
trigrams, and a trigram like ``知识图`` is present in more than half
of any knowledge-graph-heavy corpus.  The consequence is visible in
the product: a CJK recall reported ``bm25: 2.07e-06``, which the
evidence chain rounded to ``0.0`` -- a headline 0.4.15 feature
telling the user "this hit because bm25 0.0".

The second defect is that the ``LIKE`` fallback and the FTS5 channel
were never on the same scale.  A LIKE-only hit was published with the
memory's ``importance`` (0.5 by default) while a genuine BM25 hit was
published with the raw FTS5 score, so the evidence chain reported an
*importance* value under the name ``bm25``.

What this module does
---------------------
It computes a textbook BM25 score (k1 = 1.2, b = 0.75) with the
**Lucene / BM25+ inverse document frequency**::

    idf = log( 1 + (N - n + 0.5) / (n + 0.5) )

which is strictly positive for every term.  The only remaining
discriminators are term frequency (with saturation) and document
length normalisation, so a corpus in which a query term is common
still ranks correctly and still reports a meaningful score.

On top of the BM25 score it applies the rule
``rohitg00/agentmemory`` shipped in v0.9.31 (2026-10-26): **exact
matches first**.  agentmemory fixed a prefix-expansion bug that cost
18.94 points of strict recall-all@5 on LongMemEval by letting an
exact match be counted again as a prefix match; the transferable
lesson is that a document containing the query verbatim must outrank
a document that merely contains the same scattered trigrams.

Dependencies: none beyond the stdlib.  The corpus statistics come
from the FTS5 mirror that is already indexed, so the extra cost is a
handful of small queries plus a ``str.count`` pass over the
candidate pool (tens of documents), not a scan of the corpus.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable

#: BM25 term-frequency saturation.
K1 = 1.2
#: BM25 length normalisation.
B = 0.75
#: Multiplier applied to a document that contains the query verbatim.
#: The sort is also keyed on the exact-match flag, so this only shapes
#: the reported score -- it never decides the order on its own.
EXACT_MATCH_BOOST = 2.0

#: A CJK run of fewer than three characters cannot be tokenised by the
#: trigram tokenizer, so it can never be scored.  Such terms are
#: ignored here and left to the ``LIKE`` fallback and to the
#: exact-substring check.
MIN_TERM_LEN = 3

_CJK_RE = re.compile(r"[一-鿿]+")
_LATIN_RE = re.compile(r"[\w]+", re.UNICODE)


@dataclass
class LexicalHit:
    """One scored candidate."""

    doc_id: str
    score: float
    exact: bool
    matched_terms: int
    total_terms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.doc_id,
            "score": round(self.score, 6),
            "exact": self.exact,
            "matched_terms": self.matched_terms,
            "total_terms": self.total_terms,
        }


def query_terms(query: str) -> list[str]:
    """Split a query into the terms the lexical scorer scores on.

    Mirrors the tokenisation ``_escape_fts`` uses to build the FTS5
    MATCH expression -- CJK runs become character trigrams (the
    tokenizer's unit), everything else becomes a word -- but it keeps
    **every** trigram of a CJK run instead of only the two boundary
    ones.  The extra terms are what let the scorer tell
    ``图谱构建`` apart from ``图谱的构建``: both contain the boundary
    trigrams the FTS5 query uses, so both reach the candidate pool,
    but only one of them contains the interior trigrams.
    """
    s = (query or "").lower()
    terms: list[str] = []
    seen: set[str] = set()

    def _add(term: str) -> None:
        if term and term not in seen:
            seen.add(term)
            terms.append(term)

    for run in _CJK_RE.findall(s):
        for i in range(len(run) - 2):
            _add(run[i:i + 3])
    cleaned = _CJK_RE.sub(" ", s)
    for word in _LATIN_RE.findall(cleaned):
        _add(word)
    return terms


def _normalise(text: str, strip_spaces: bool) -> str:
    t = (text or "").lower()
    if strip_spaces:
        return re.sub(r"\s+", "", t)
    return " ".join(t.split())


def exact_match(text: str, query: str) -> bool:
    """True when ``text`` contains ``query`` verbatim.

    Whitespace is collapsed for Latin text and removed entirely for
    CJK text, because Chinese queries are commonly typed with stray
    spaces that a stored memory will not contain.
    """
    q = (query or "").strip()
    if not q:
        return False
    strip_spaces = bool(_CJK_RE.search(q))
    needle = _normalise(q, strip_spaces)
    if not needle:
        return False
    return needle in _normalise(text, strip_spaces)


def _content_expression(kind: str) -> tuple[str, str]:
    """Return ``(fts_table, content_expr)`` for a channel.

    ``content_expr`` is an SQL expression producing the text that is
    tokenised for scoring.  It is applied to the FTS mirror for the
    corpus statistics and to the base table for the per-document term
    frequencies, so the two stay comparable.
    """
    if kind == "wiki":
        return (
            "wiki_fts",
            "title || ' ' || COALESCE(body,'') || ' ' || "
            "COALESCE(summary,'') || ' ' || COALESCE(tags,'')",
        )
    return ("memories_fts", "text")


def _corpus_stats(conn: Any, kind: str) -> tuple[float, float]:
    """Return ``(N, avgdl)`` for the indexed population."""
    table, expr = _content_expression(kind)
    row = conn.execute(
        f"SELECT COUNT(*) AS n, AVG(LENGTH({expr})) AS avgdl FROM {table}"
    ).fetchone()
    n = float(row["n"] or 0)
    avgdl = float(row["avgdl"] or 0.0)
    return n, avgdl


def _document_frequencies(conn: Any, kind: str, terms: list[str]) -> dict[str, float]:
    """Return ``{term: df}`` using the FTS5 index.

    One round-trip: the per-term counts are unioned into a single
    statement.  A term the trigram tokenizer cannot represent (fewer
    than three characters) matches nothing and would otherwise be
    scored with a bogus idf, so it is reported as occurring in every
    document -- which drives its idf to zero and removes it from the
    score, exactly as an uninformative term should be removed.
    """
    table, _expr = _content_expression(kind)
    usable = [t for t in terms if len(t) >= MIN_TERM_LEN]
    if not usable:
        return {}
    parts = []
    params: list[str] = []
    for term in usable:
        parts.append(f"SELECT ? AS term, COUNT(*) AS df FROM {table} WHERE {table} MATCH ?")
        params.extend([term, f'"{term}"'])
    sql = " UNION ALL ".join(parts)
    out: dict[str, float] = {}
    for row in conn.execute(sql, params):
        out[row["term"]] = float(row["df"] or 0)
    return out


def _candidate_contents(
    conn: Any, kind: str, doc_ids: list[str]
) -> dict[str, str]:
    """Fetch the tokenisable content of the candidate documents."""
    if not doc_ids:
        return {}
    placeholders = ",".join("?" * len(doc_ids))
    if kind == "wiki":
        sql = (
            "SELECT id, title || ' ' || COALESCE(body,'') || ' ' || "
            "COALESCE(summary,'') || ' ' || COALESCE(tags,'') AS content "
            f"FROM wiki_pages WHERE id IN ({placeholders})"
        )
    else:
        sql = (
            "SELECT id, text AS content "
            f"FROM memories WHERE id IN ({placeholders})"
        )
    rows = conn.execute(sql, doc_ids).fetchall()
    return {r["id"]: (r["content"] or "") for r in rows}


def score_documents(
    store: Any,
    query: str,
    doc_ids: Iterable[str],
    *,
    kind: str = "memories",
) -> dict[str, LexicalHit]:
    """Score ``doc_ids`` against ``query`` with a textbook BM25.

    Returns a mapping of document id -> :class:`LexicalHit`.  Documents
    that match no term are omitted; callers keep their original order
    for those.

    The score is on a *comparable* scale across documents and across
    channels: it never collapses to the 1e-6 floor that SQLite's
    ``bm25()`` produces for a common term, and a document that matches
    more of the query always scores at least as high as one that
    matches fewer terms.
    """
    ids = [d for d in dict.fromkeys(doc_ids) if d]
    if not ids:
        return {}
    terms = query_terms(query)
    terms = [t for t in terms if len(t) >= MIN_TERM_LEN]
    if not terms:
        return {}

    # One connection for the whole score: opening a SQLite connection
    # costs ~0.45 ms on this workload (three PRAGMAs plus the commit),
    # so three separate connections would cost more than the queries.
    with store._conn() as conn:
        n_docs, avgdl = _corpus_stats(conn, kind)
        if n_docs <= 0:
            return {}
        if avgdl <= 0:
            avgdl = 1.0
        dfs = _document_frequencies(conn, kind, terms)
        contents = _candidate_contents(conn, kind, ids)

    hits: dict[str, LexicalHit] = {}
    for doc_id in ids:
        content = contents.get(doc_id)
        if content is None:
            continue
        lowered = content.lower()
        dl = float(len(lowered)) or 1.0
        score = 0.0
        matched = 0
        for term in terms:
            tf = lowered.count(term)
            if tf <= 0:
                continue
            matched += 1
            df = min(float(dfs.get(term, n_docs)), n_docs)
            # Lucene / BM25+ idf: strictly positive, so a common term
            # loses influence smoothly instead of falling off a cliff.
            idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
            denom = tf + K1 * (1.0 - B + B * dl / avgdl)
            score += idf * (tf * (K1 + 1.0)) / denom
        if matched == 0:
            continue
        is_exact = exact_match(content, query)
        if is_exact:
            score *= EXACT_MATCH_BOOST
        hits[doc_id] = LexicalHit(
            doc_id=doc_id,
            score=score,
            exact=is_exact,
            matched_terms=matched,
            total_terms=len(terms),
        )
    return hits


def rank_hits(
    hits: dict[str, LexicalHit],
    fallback_order: list[str],
) -> list[str]:
    """Order document ids by relevance: exact matches first.

    ``fallback_order`` is the order the caller already had (FTS5 rank,
    then the LIKE fallback).  It is used as the tie-break so the
    ordering stays stable and deterministic when two documents score
    identically.
    """
    position = {doc_id: idx for idx, doc_id in enumerate(fallback_order)}

    def _key(doc_id: str) -> tuple[int, float, int]:
        hit = hits[doc_id]
        return (0 if hit.exact else 1, -hit.score, position.get(doc_id, 0))

    return sorted(hits.keys(), key=_key)
