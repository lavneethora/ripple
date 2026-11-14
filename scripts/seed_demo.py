"""Build the committed Relate store from real OpenAlex and Crossref data.

    python scripts/seed_demo.py

Re-runnable: HTTP responses are cached under data/raw/ and every write is a
MERGE, so running it twice changes nothing.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from ripple.graph.actor import GraphActor  # noqa: E402
from ripple.ingest.seed import SeedPlan, seed  # noqa: E402
from ripple.ingest.sources import Fetcher  # noqa: E402

STORE = ROOT / "data" / "ripple-retractions"
CACHE = ROOT / "data" / "raw"


def main() -> int:
    fresh = "--fresh" in sys.argv
    if fresh and STORE.exists():
        print(f"Removing existing store at {STORE}")
        shutil.rmtree(STORE)

    STORE.parent.mkdir(parents=True, exist_ok=True)
    fetcher = Fetcher(CACHE)

    print(f"Store: {STORE}")
    print(f"Cache: {CACHE}\n")

    with GraphActor(STORE, readers=1) as actor:
        report = seed(actor, fetcher, SeedPlan())

        print("\n--- store contents ---")
        for label in ("Work", "Author", "RetractionEvent"):
            n = actor.write(f"MATCH (n:{label}) RETURN count(n) AS n")[0]["n"]
            print(f"  {label:<16} {n}")
        rels = actor.write(
            "MATCH ()-[r]->() RETURN type(r) AS t, count(r) AS n ORDER BY t"
        )
        for row in rels:
            print(f"  {row['t']:<16} {row['n']}")

        print("\n--- post-retraction citations in the store ---")
        rows = actor.write(
            "MATCH (citing:Work)-[r:CITES]->(w:Work {openalex_id: $id}) "
            "WHERE r.post_retraction_lag_days > 0 AND r.awareness = 'unambiguous' "
            "RETURN citing.openalex_id AS id, citing.type AS type, "
            "       citing.pub_date AS published, r.post_retraction_lag_days AS lag, "
            "       citing.title AS title "
            "ORDER BY lag DESC LIMIT 8",
            {"id": SeedPlan().focal_work},
        )
        for row in rows:
            print(f"  +{row['lag']:>4}d  {row['published']}  {row['type']:<8} {row['title'][:52]}")

    print(f"\ncache: {report.cache_hits} hits, {report.cache_misses} misses")
    print(f"unambiguous post-retraction citations: {report.unambiguous_post_retraction}")
    if report.skipped_over_budget:
        print(f"skipped over budget: {report.skipped_over_budget}")
    print("\nInspect without running the app:")
    print(f"  relate --database {STORE.relative_to(ROOT)} --read-only "
          '"MATCH (n) RETURN count(n) AS n"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
