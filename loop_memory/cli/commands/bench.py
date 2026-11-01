"""``loop-memory bench`` -- deterministic retrieval-quality benchmark.

Audit 2026-11-01 (loop-memory 0.4.18).

Loop Memory had hybrid retrieval but no way to *measure* whether the
lexical channel ranked anything sensibly.  This command scores the
lexical channel against a fixed, versioned fixture and prints the
result as JSON, so a ranking change can be reviewed the way a test
failure would be.

The methodology follows ``rohitg00/agentmemory`` v0.9.31 (2026-10-26):
a fixed corpus with an explicit revision, the SHA-256 of the fixture
bytes recorded in every report, and an explicit statement of what the
numbers do *not* prove.

The run is fully offline and calls no provider API.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _print_text(report: dict, comparison: dict | None = None) -> None:
    """Human-readable rendering of a benchmark report."""
    lines = []
    lines.append(
        f"fixture : {report['fixture_name']} (revision {report['fixture_revision']})"
    )
    lines.append(f"sha256  : {report['fixture_sha256']}")
    lines.append(f"top-k   : {report['top_k']}")
    lines.append("")
    lines.append("metrics")
    for key in sorted(report.get("metrics") or {}):
        lines.append(f"  {key:<12} {report['metrics'][key]:.4f}")
    scale = report.get("score_scale") or [0, 0]
    flag = "  <-- degenerate scale" if report.get("degenerate_score_scale") else ""
    lines.append("")
    lines.append(f"score scale : {scale[0]:.6g} .. {scale[1]:.6g}{flag}")
    lines.append("")
    lines.append("per query")
    for q in report.get("queries") or []:
        rank = q.get("first_relevant_rank")
        rank_txt = f"rank {rank}" if rank is not None else "not in top-k"
        lines.append(
            f"  {q['query_id']:<4} {q['query'][:34]:<34} "
            f"rr={q['reciprocal_rank']:.3f} ndcg={q['ndcg']:.3f} {rank_txt}"
        )
    if comparison is not None:
        lines.append("")
        lines.append("comparison vs baseline")
        if not comparison.get("comparable"):
            lines.append("  NOT COMPARABLE: the two runs used different fixtures.")
        for key in sorted(comparison.get("metrics") or {}):
            delta = comparison["metrics"][key]
            sign = "+" if delta >= 0 else ""
            lines.append(f"  {key:<12} {sign}{delta:.4f}")
    lines.append("")
    lines.append("limitations")
    for item in report.get("limitations") or []:
        lines.append(f"  - {item}")
    print("\n".join(lines))


def run_bench(args: list) -> int:
    """``loop-memory bench [--fixture PATH] [--json] [--out PATH] [--compare PATH]``"""
    p = argparse.ArgumentParser(prog="loop-memory bench")
    p.add_argument(
        "--fixture",
        default=None,
        help="path to a benchmark fixture (defaults to the packaged fixture)",
    )
    p.add_argument("--top-k", type=int, default=10, help="results to consider per query")
    p.add_argument("--json", action="store_true", help="print the raw JSON report")
    p.add_argument("--out", default=None, help="write the JSON report to this path")
    p.add_argument(
        "--compare",
        default=None,
        help="path to a previous JSON report; prints the per-metric delta",
    )
    ns = p.parse_args(args)

    from ...bench.runner import compare_reports, load_fixture, run_benchmark

    try:
        fixture, digest = load_fixture(ns.fixture)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    report = run_benchmark(
        fixture, fixture_sha256=digest, top_k=ns.top_k
    ).to_dict()

    comparison = None
    if ns.compare:
        try:
            baseline = json.loads(Path(ns.compare).expanduser().read_text("utf-8"))
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            print(f"cannot read baseline report: {exc}", file=sys.stderr)
            return 2
        comparison = compare_reports(baseline, report)

    if ns.out:
        out_path = Path(ns.out).expanduser()
        payload = {"report": report}
        if comparison is not None:
            payload["comparison"] = comparison
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if ns.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        _print_text(report, comparison)
    return 0
