"""Run the Graphify adapter against a real GitHub repo and snapshot results.

The blueprint referenced `graphify . --mode deep --output services/<svc>/graph/`
as a build-time step. In Airen there is no separate `graphify` CLI — the
"graph queries" are done LIVE by `RealGraphifyAdapter` (ripgrep + Python ast).
This script triggers the adapter against a real repo so we have a tangible
artifact for the demo and to confirm the adapter works end-to-end on the
real FK codebase (not just the canned mock data).

Usage:
    python -m scripts.run_graphify \\
        --repo cloudqwest/dynamic_eta_prediction \\
        --service tl-eta \\
        --symbol fetch_historical_checkcalls \\
        --constant api_fetch_limit

Output written to: services/<service>/graph/airen-graphify-output.json
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _timed(fn, *args, **kwargs):
    """Run fn and return (result, elapsed_ms)."""
    t0 = time.perf_counter()
    out = fn(*args, **kwargs)
    return out, int((time.perf_counter() - t0) * 1000)


def main() -> None:
    load_dotenv(override=True)
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", required=True, help="owner/repo, e.g. cloudqwest/dynamic_eta_prediction")
    p.add_argument("--service", required=True, help="Service name (controls output path)")
    p.add_argument("--symbol", default="fetch_historical_checkcalls",
                   help="Function/method to look up definition + callers for")
    p.add_argument("--constant", default="api_fetch_limit",
                   help="Constant assignment to scan for")
    args = p.parse_args()

    # Force real mode for this run regardless of env
    from airen.adapters.graphify_real import RealGraphifyAdapter

    print(f"📂 Booting Graphify against {args.repo!r}")
    try:
        adapter = RealGraphifyAdapter()
    except RuntimeError as e:
        raise SystemExit(f"❌ {e}")

    # Force the clone now so we see the timing
    print(f"   Cloning / refreshing repo cache…")
    t0 = time.perf_counter()
    repo_path = adapter._repo_path(args.repo)
    clone_ms = int((time.perf_counter() - t0) * 1000)
    print(f"   ✓ Local path: {repo_path}  ({clone_ms} ms)")

    # 1) Graph summary
    summary, ms_summary = _timed(adapter.graph_summary, args.repo)
    print(f"\n📊 graph_summary  ({ms_summary} ms)")
    print(f"   Python files: {summary['n_python_files']:,}")

    # 2) find_definition
    definition, ms_def = _timed(adapter.find_definition, args.repo, args.symbol)
    print(f"\n🎯 find_definition({args.symbol!r})  ({ms_def} ms)")
    if "error" in definition:
        print(f"   ⚠  {definition['error']}")
    else:
        print(f"   {definition['kind']}  at  {definition['file']}:{definition['line']}")
        print(f"      → {definition['snippet'][:90]}")

    # 3) find_callers
    callers, ms_callers = _timed(adapter.find_callers, args.repo, args.symbol)
    print(f"\n🔗 find_callers({args.symbol!r})  →  {len(callers)} call sites  ({ms_callers} ms)")
    for c in callers[:6]:
        print(f"   {c['file']}:{c['line']}  → {c['snippet'][:80]}")
    if len(callers) > 6:
        print(f"   … and {len(callers) - 6} more")

    # 4) find_constant_assignments — the smoking-gun query for Fritolay
    constants, ms_const = _timed(adapter.find_constant_assignments, args.repo, args.constant)
    print(f"\n💣 find_constant_assignments({args.constant!r})  →  {len(constants)} sites  ({ms_const} ms)")
    for c in constants[:10]:
        print(f"   {c['file']}:{c['line']}  → {c['snippet'][:80]}")
    if len(constants) > 10:
        print(f"   … and {len(constants) - 10} more")

    # ── Persist ──
    output = {
        "service": args.service,
        "repo": args.repo,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "local_path": str(repo_path),
        "timings_ms": {
            "clone_or_refresh": clone_ms,
            "summary": ms_summary,
            "find_definition": ms_def,
            "find_callers": ms_callers,
            "find_constant_assignments": ms_const,
        },
        "summary": summary,
        "queries": {
            "find_definition": {
                "symbol": args.symbol,
                "result": definition,
            },
            "find_callers": {
                "symbol": args.symbol,
                "n_results": len(callers),
                "results": callers,
            },
            "find_constant_assignments": {
                "symbol": args.constant,
                "n_results": len(constants),
                "results": constants,
            },
        },
    }

    out_dir = ROOT / "services" / args.service / "graph"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "airen-graphify-output.json"
    out_path.write_text(json.dumps(output, indent=2))
    print(f"\n✅ Wrote {out_path.relative_to(ROOT)}  ({out_path.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
