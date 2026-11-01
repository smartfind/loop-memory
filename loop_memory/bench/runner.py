"""Deterministic retrieval-quality benchmark runner.

Audit 2026-11-01 (loop-memory 0.4.18).

Measures the **ranking** quality of the BM25 / lexical channel, not
answer accuracy.  The numbers are computed over a fixed synthetic
corpus so two runs are directly comparable; the fixture's SHA-256 is
recorded in every report so a reader can tell exactly which bytes
produced a score.

Metrics
-------
``recall@k``
    Fraction of the judged-relevant documents that appear in the top
    ``k`` results (``k`` in 1, 3, 5).  Macro-averaged over queries.
``mrr@10``
    Mean reciprocal rank of the *first* relevant hit within the top
    10.  ``1.0`` means the right answer was ranked first every time.
``ndcg@10``
    Normalised discounted cumulative gain with binary gains -- a
    stricter view that also rewards putting *all* relevant hits high.
``score_scale``
    ``(min, max)`` of the raw channel score over the whole run.  This
    is a *diagnostic*, not a quality metric: SQLite's ``bm25()``
    clamps a non-positive inverse document frequency to ``1e-6``, so a
    corpus in which a query term is common collapses the reported
    scores to ~1e-6 while the *ordering* survives.  A run whose
    ``score_scale`` max is below 1e-4 is reporting a degenerate scale
    even if the ranking happens to look fine.

Limitations (deliberate, and stated in every report)
----------------------------------------------------
* Synthetic corpus.  These are **development retrieval results**, not
  held-out answer accuracy on a real user corpus.
* No provider API is called; the run is offline and costs $0.
* Only the lexical channel is measured.  The semantic channel needs an
  embedding model and the entity channel needs a graph build, so
  neither is included -- a benchmark that silently depends on a
  downloaded model would not be reproducible.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_TOP_K = 10
DEFAULT_K_CUTS = (1, 3, 5)

#: A reported channel score below this counts as "degenerate scale".
#: SQLite's FTS5 ``bm25()`` floors a non-positive idf at 1e-6, and a
#: single clamped term in a two-term query keeps the whole score near
#: that floor.  1e-4 leaves a wide margin over that floor while still
#: flagging a real collapse.
DEGENERATE_SCORE_FLOOR = 1e-4


@dataclass
class QueryResult:
    """Per-query outcome of one benchmark run."""

    query_id: str
    query: str
    channel: str
    relevant: list[str]
    ranked: list[str]
    recall_at: dict[int, float] = field(default_factory=dict)
    reciprocal_rank: float = 0.0
    ndcg: float = 0.0
    first_relevant_rank: int | None = None
    score_min: float = 0.0
    score_max: float = 0.0
    fts_score_min: float = 0.0
    fts_score_max: float = 0.0
    degenerate_fts_score: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "query": self.query,
            "channel": self.channel,
            "relevant": list(self.relevant),
            "ranked": list(self.ranked),
            "recall_at": {str(k): round(v, 6) for k, v in self.recall_at.items()},
            "reciprocal_rank": round(self.reciprocal_rank, 6),
            "ndcg": round(self.ndcg, 6),
            "first_relevant_rank": self.first_relevant_rank,
            "score_min": self.score_min,
            "score_max": self.score_max,
            "fts_score_min": self.fts_score_min,
            "fts_score_max": self.fts_score_max,
            "degenerate_fts_score": self.degenerate_fts_score,
            "note": self.note,
        }


@dataclass
class BenchmarkReport:
    """Aggregate outcome of one benchmark run."""

    fixture_name: str
    fixture_revision: int
    fixture_sha256: str
    top_k: int
    queries: list[QueryResult] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    score_scale: tuple[float, float] = (0.0, 0.0)
    degenerate_score_scale: bool = False
    fts_score_scale: tuple[float, float] = (0.0, 0.0)
    degenerate_fts_queries: int = 0
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixture_name": self.fixture_name,
            "fixture_revision": self.fixture_revision,
            "fixture_sha256": self.fixture_sha256,
            "top_k": self.top_k,
            "metrics": {k: round(v, 6) for k, v in self.metrics.items()},
            "score_scale": [self.score_scale[0], self.score_scale[1]],
            "degenerate_score_scale": self.degenerate_score_scale,
            "fts_score_scale": [self.fts_score_scale[0], self.fts_score_scale[1]],
            "degenerate_fts_queries": self.degenerate_fts_queries,
            "queries": [q.to_dict() for q in self.queries],
            "limitations": list(self.limitations),
        }


def default_fixture_path() -> Path:
    """Absolute path of the fixture shipped with the package.

    Audit 2026-11-01 (loop-memory 0.4.19): the fixture is *package
    data*.  It was not declared in ``pyproject.toml``, so the 0.4.18
    wheel shipped without it and ``loop-memory bench`` died with a
    ``FileNotFoundError`` on every PyPI install.  ``importlib.resources``
    is the fallback for installs whose files are not reachable through
    ``__file__``.
    """
    direct = Path(__file__).resolve().parent / "fixtures" / "default.json"
    if direct.is_file():
        return direct
    from importlib.resources import files

    return Path(str(files("loop_memory.bench").joinpath("fixtures").joinpath("default.json")))


def load_fixture(path: str | Path | None = None) -> tuple[dict[str, Any], str]:
    """Load a benchmark fixture.

    Returns ``(fixture, sha256)`` where ``sha256`` hashes the *raw
    file bytes* -- the report therefore pins the exact input, not a
    re-serialisation of it.
    """
    p = Path(path).expanduser() if path else default_fixture_path()
    try:
        raw = p.read_bytes()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"benchmark fixture not found: {p}\n"
            "The default fixture ships as package data; if you installed "
            "loop-memory from PyPI and this is the packaged fixture, the "
            "distribution is missing it (that was the 0.4.18 bug fixed "
            "in 0.4.19). Pass --fixture PATH to point at your own."
        ) from exc
    digest = hashlib.sha256(raw).hexdigest()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"fixture {p} must contain a JSON object")
    data.setdefault("name", p.stem)
    data.setdefault("revision", 1)
    data.setdefault("corpus", [])
    data.setdefault("wiki", [])
    data.setdefault("queries", [])
    return data, digest


def _recall(ranked: list[str], relevant: list[str], k: int) -> float:
    if not relevant:
        return 0.0
    top = set(ranked[:k])
    hit = sum(1 for r in relevant if r in top)
    return hit / float(len(relevant))


def _reciprocal_rank(ranked: list[str], relevant: list[str], top_k: int) -> tuple[float, int | None]:
    rel = set(relevant)
    for idx, doc_id in enumerate(ranked[:top_k], start=1):
        if doc_id in rel:
            return 1.0 / idx, idx
    return 0.0, None


def _ndcg(ranked: list[str], relevant: list[str], top_k: int) -> float:
    if not relevant:
        return 0.0
    rel = set(relevant)
    dcg = 0.0
    for idx, doc_id in enumerate(ranked[:top_k], start=1):
        if doc_id in rel:
            dcg += 1.0 / math.log2(idx + 1)
    ideal_hits = min(len(rel), top_k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return (dcg / idcg) if idcg > 0 else 0.0


def _ingest(store: Any, fixture: dict[str, Any]) -> dict[str, str]:
    """Load the fixture into ``store``; return a slug->id map."""
    for row in fixture.get("corpus") or []:
        store.upsert_memory(
            id=row["id"],
            kind=row.get("kind", "fact"),
            text=row["text"],
            importance=float(row.get("importance", 0.5)),
            tags=list(row.get("tags") or []),
            source=row.get("source"),
        )
    slug_to_id: dict[str, str] = {}
    for row in fixture.get("wiki") or []:
        store.upsert_wiki_page(
            slug=row["slug"],
            title=row.get("title", row["slug"]),
            body=row.get("body", ""),
            summary=row.get("summary"),
            tags=list(row.get("tags") or []),
            importance=float(row.get("importance", 0.5)),
        )
        page = store.get_wiki_page_by_slug(row["slug"])
        if page:
            slug_to_id[row["slug"]] = page["id"]
    return slug_to_id


def run_benchmark(
    fixture: dict[str, Any] | None = None,
    *,
    fixture_path: str | Path | None = None,
    fixture_sha256: str | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> BenchmarkReport:
    """Run the benchmark and return a :class:`BenchmarkReport`.

    The store is built in a throwaway temporary directory so the
    benchmark never touches the user's real database.
    """
    from ..storage.retrieval import bm25_search
    from ..storage.sqlite_store import MemoryStore

    if fixture is None:
        fixture, digest = load_fixture(fixture_path)
    else:
        digest = fixture_sha256 or ""
    if not digest:
        raw = json.dumps(fixture, ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()

    tmpdir = tempfile.mkdtemp(prefix="loop-memory-bench-")
    try:
        store = MemoryStore(Path(tmpdir) / "bench.db")
        slug_to_id = _ingest(store, fixture)

        results: list[QueryResult] = []
        score_min = float("inf")
        score_max = float("-inf")
        fts_min = float("inf")
        fts_max = float("-inf")

        for q in fixture.get("queries") or []:
            channel = q.get("channel", "memories")
            kind = "wiki" if channel == "wiki" else "memories"
            relevant = []
            for ref in q.get("relevant") or []:
                relevant.append(slug_to_id.get(ref, ref))
            hits = bm25_search(store, q["query"], kind=kind, limit=top_k)
            ranked = [h["id"] for h in hits]
            scores = [float(h.get("_score") or 0.0) for h in hits]
            if scores:
                score_min = min(score_min, min(scores))
                score_max = max(score_max, max(scores))
            fts_scores = [
                float(h["_fts_score"])
                for h in hits
                if h.get("_fts_score") is not None
            ]
            if fts_scores:
                fts_min = min(fts_min, min(fts_scores))
                fts_max = max(fts_max, max(fts_scores))
            degenerate_fts = bool(fts_scores) and max(fts_scores) < DEGENERATE_SCORE_FLOOR
            rr, first_rank = _reciprocal_rank(ranked, relevant, top_k)
            results.append(
                QueryResult(
                    query_id=q.get("id", q["query"]),
                    query=q["query"],
                    channel=channel,
                    relevant=relevant,
                    ranked=ranked,
                    recall_at={k: _recall(ranked, relevant, k) for k in DEFAULT_K_CUTS},
                    reciprocal_rank=rr,
                    ndcg=_ndcg(ranked, relevant, top_k),
                    first_relevant_rank=first_rank,
                    score_min=min(scores) if scores else 0.0,
                    score_max=max(scores) if scores else 0.0,
                    fts_score_min=min(fts_scores) if fts_scores else 0.0,
                    fts_score_max=max(fts_scores) if fts_scores else 0.0,
                    degenerate_fts_score=degenerate_fts,
                    note=q.get("note", ""),
                )
            )

        n = len(results) or 1
        metrics = {
            f"recall@{k}": sum(r.recall_at[k] for r in results) / n for k in DEFAULT_K_CUTS
        }
        metrics[f"mrr@{top_k}"] = sum(r.reciprocal_rank for r in results) / n
        metrics[f"ndcg@{top_k}"] = sum(r.ndcg for r in results) / n

        if score_min is float("inf"):
            score_min = score_max = 0.0
        if fts_min is float("inf"):
            fts_min = fts_max = 0.0

        return BenchmarkReport(
            fixture_name=fixture.get("name", "unknown"),
            fixture_revision=int(fixture.get("revision", 1)),
            fixture_sha256=digest,
            top_k=top_k,
            queries=results,
            metrics=metrics,
            score_scale=(score_min, score_max),
            degenerate_score_scale=score_max < DEGENERATE_SCORE_FLOOR,
            fts_score_scale=(fts_min, fts_max),
            degenerate_fts_queries=sum(1 for r in results if r.degenerate_fts_score),
            limitations=[
                "Synthetic corpus: these are development retrieval results, "
                "not held-out answer accuracy on a real user corpus.",
                "No provider API is called; the run is offline and costs $0.",
                "Only the lexical channel is measured; the semantic channel "
                "needs an embedding model and the entity channel needs a "
                "graph build, so neither is included.",
            ],
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def compare_reports(
    baseline: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    """Diff two report dicts (baseline first, current second).

    Returns the per-metric deltas plus the fixture identity check.  A
    comparison across *different* fixtures is meaningless, so the
    result says so explicitly instead of printing a bogus delta.
    """
    same_fixture = (
        baseline.get("fixture_sha256") == current.get("fixture_sha256")
        and baseline.get("fixture_revision") == current.get("fixture_revision")
    )
    base_metrics = baseline.get("metrics") or {}
    curr_metrics = current.get("metrics") or {}
    deltas: dict[str, float] = {}
    for key in sorted(set(base_metrics) | set(curr_metrics)):
        deltas[key] = round(float(curr_metrics.get(key, 0.0)) - float(base_metrics.get(key, 0.0)), 6)
    return {
        "comparable": same_fixture,
        "baseline_fixture": {
            "name": baseline.get("fixture_name"),
            "revision": baseline.get("fixture_revision"),
            "sha256": baseline.get("fixture_sha256"),
        },
        "current_fixture": {
            "name": current.get("fixture_name"),
            "revision": current.get("fixture_revision"),
            "sha256": current.get("fixture_sha256"),
        },
        "metrics": deltas,
    }
