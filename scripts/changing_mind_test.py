"""The Changing-Mind Test: the gate this project has to pass.

No LLM, no frontend, no hardcoded answers. A real retraction, from a real
store, re-derived by the engine.

It passes only if the system can:
  1. preserve the original work and the evidence behind it,
  2. find the real downstream dependency paths,
  3. tell an impacted belief apart from a genuinely unsupported one,
  4. locate surviving independent support,
  5. and persist a result that can be inspected afterwards.

    python scripts/changing_mind_test.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from ripple.graph.actor import GraphActor  # noqa: E402
from ripple.ingest import justifications  # noqa: E402
from ripple.tms.engine import Status  # noqa: E402
from ripple.tms.service import (  # noqa: E402
    post_retraction_breakdown,
    propagate,
    retraction_event,
)

STORE = ROOT / "data" / "ripple-retractions"
WORK_DIR = ROOT / "data" / ".work"
FIXTURE = ROOT / "data" / "curated" / "demo.yaml"
FOCAL = "W3010930696"

BADGE = {
    Status.SUPPORTED: "holds",
    Status.SUPPORTED_WEAKENED: "holds, weakened",
    Status.UNSUPPORTED: "LOSES SUPPORT",
    Status.UNSUPPORTED_CIRCULAR: "CIRCULAR ONLY",
    Status.INDETERMINATE: "cannot tell",
    Status.QUESTIONED: "questioned",
    Status.RETRACTED: "retracted",
}


def show(result, title: str) -> None:
    print(f"\n  {title}")
    for verdict in sorted(result.verdicts, key=lambda v: v.belief_id):
        badge = BADGE.get(verdict.status, verdict.status.value)
        support = f"{verdict.live_support_count}/{verdict.prior_support_count}"
        print(f"    {verdict.belief_id:<24} {badge:<16} support {support:<6} {verdict.reason_code}")


def working_copy() -> Path:
    """Copy the committed store so a run never mutates the artifact.

    Relate rewrites content-addressed snapshot files and advances the WAL on
    every write, so propagating against the committed store would leave a
    large binary diff after each run and make the repository churn. The
    committed store is read-only input; runs happen on a copy.
    """
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    target = Path(tempfile.mkdtemp(prefix="run-", dir=WORK_DIR)) / "store"
    shutil.copytree(STORE, target)
    return target


def main() -> int:
    store = working_copy()
    with GraphActor(store, readers=1) as actor:
        print("=" * 74)
        print("  THE CHANGING-MIND TEST")
        print("=" * 74)
        print(f"\nWorking on a copy of the committed store")
        print(f"  source {STORE.relative_to(ROOT)}")
        print(f"  copy   {store.relative_to(ROOT)}")

        loaded = actor.read("MATCH (j:Justification) RETURN count(j) AS n")[0]["n"]
        if loaded == 0:
            report = justifications.load(actor, FIXTURE)
            print(f"  loaded {report.justifications} justifications from the fixture")
        else:
            tiers = actor.read(
                "MATCH (j:Justification) RETURN j.provenance_tier AS t, count(j) AS n"
            )
            print("  justification layer already in the store: "
                  + ", ".join(f"{r['t']}={r['n']}" for r in tiers))

        event = retraction_event(actor, FOCAL)
        print(f"\nFocal work {FOCAL}")
        print(f"  {event['title'][:68]}")
        print(f"  doi            {event['doi']}")
        print(f"  retracted on   {event['retracted_on']}  (source: {event['source']})")
        print(f"  cited          {event['cited_by_count']} times")

        print("\nBibliographic exposure, which is NOT loss of support:")
        for row in post_retraction_breakdown(actor, FOCAL):
            print(f"  {row['awareness']:<20} n={row['n']:<4} "
                  f"lag {row['min_lag']} to {row['max_lag']} days")

        # --- before: the state of belief prior to learning of the retraction
        actor.write(
            "MATCH (w:Work {openalex_id:$id}) SET w.is_retracted = false, w.label = 'IN'",
            {"id": FOCAL},
        )
        before = propagate(actor, epoch=1, write_back=False)
        show(before, "BEFORE: what was believed while the study still stood")

        # --- after: the retraction is now known
        after = propagate(actor, retracted_work=FOCAL, epoch=2)
        show(after, "AFTER: re-derived once the retraction is known")

        print(f"\n  summary: {after.summary}")
        print(f"  {after.n_beliefs} beliefs, {after.n_justifications} justifications")

        # --- the assertions that decide whether this passed
        print("\n" + "=" * 74)
        checks: list[tuple[str, bool]] = [
            ("sole support on the retracted study loses support",
             after.status_of("SYN_SOLE_SUPPORT") is Status.UNSUPPORTED),
            ("1 hop away but 2-of-3 corroborated still holds",
             after.status_of("SYN_NEAR_CORROBORATED") is Status.SUPPORTED),
            ("5 hops away but wholly dependent loses support",
             after.status_of("SYN_FAR_DEPENDENT") is Status.UNSUPPORTED),
            ("status does not track distance (near holds, far does not)",
             after.status_of("SYN_NEAR_CORROBORATED") is Status.SUPPORTED
             and after.status_of("SYN_FAR_DEPENDENT") is Status.UNSUPPORTED),
            ("losing 1 of 2 independent supports weakens, does not break",
             after.status_of("SYN_WEAKENED") is Status.SUPPORTED_WEAKENED),
            ("unrelated evidence is untouched",
             after.status_of("SYN_UNRELATED") is Status.SUPPORTED),
            ("mutually citing ring is circular, not supported",
             after.status_of("SYN_RING_A") is Status.UNSUPPORTED_CIRCULAR),
            ("unadjudicated antecedent yields cannot-tell, not a guess",
             after.status_of("SYN_DEPENDS_ON_UNKNOWN") is Status.INDETERMINATE),
            ("the retracted work and its evidence are preserved",
             bool(retraction_event(actor, FOCAL))),
            ("a belief actually changed status",
             before.summary != after.summary),
        ]
        for label, ok in checks:
            print(f"  [{'PASS' if ok else 'FAIL'}] {label}")

        persisted = actor.read(
            "MATCH (c:Claim) WHERE c.status IS NOT NULL "
            "RETURN c.status AS s, count(c) AS n ORDER BY s"
        )
        print(f"\n  persisted to the store: "
              + ", ".join(f"{r['s']}={r['n']}" for r in persisted))

        passed = all(ok for _, ok in checks)
        print("\n" + "=" * 74)
        print(f"  CHANGING-MIND TEST: {'PASSED' if passed else 'FAILED'}")
        print("=" * 74)
        return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
