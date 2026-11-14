"""Wiring between the Relate store and the in-memory truth maintenance engine.

The shape of a propagation:

  1. Read the justification layer out of Relate in one query.
  2. Label it in Python, because Cypher has no recursive fixpoint.
  3. Write the verdicts back.

Step 1 is deliberately a single read rather than a traversal loop. Cypher
cannot express the fixpoint, and a path-returning variable-length query over
a cyclic citation graph is exponential.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ripple.graph.queries import (
    Q_ALL_CLAIM_IDS,
    Q_MARK_RETRACTED_OUT,
    Q_POST_RETRACTION_BREAKDOWN,
    Q_PULL_PROJECTION,
    Q_RETRACTION_EVENT,
    Q_WRITEBACK_STATUS,
)
from ripple.tms.engine import (
    Boundary,
    Justification,
    Projection,
    Status,
    Verdict,
    classify,
)

#: Statuses that mean the belief currently holds.
HOLDS = frozenset({Status.SUPPORTED, Status.SUPPORTED_WEAKENED, Status.QUESTIONED})


def label_for(status: Status) -> str:
    """Map a verdict onto the three-valued label stored on the node.

    INDETERMINATE must round-trip as UNKNOWN rather than collapsing to OUT.
    Flattening it loses the distinction between "we assessed this and it does
    not hold" and "we have not assessed this", and because the label is the
    input to the next propagation, the loss is permanent: the belief is
    treated as refuted from then on and INDETERMINATE can never reappear.
    """
    if status in HOLDS:
        return "IN"
    if status is Status.INDETERMINATE:
        return "UNKNOWN"
    return "OUT"


@dataclass
class PropagationResult:
    epoch: int
    retracted_work: str | None
    verdicts: list[Verdict]
    summary: dict[str, int]
    n_justifications: int
    n_beliefs: int

    def by_status(self, status: Status) -> list[Verdict]:
        return [v for v in self.verdicts if v.status is status]

    def status_of(self, belief_id: str) -> Status | None:
        for v in self.verdicts:
            if v.belief_id == belief_id:
                return v.status
        return None


def _boundary_from_row(antecedent: dict[str, Any]) -> Boundary | None:
    """Map a stored belief's label onto a fixpoint boundary condition.

    A retracted work is OUT regardless of its stored label, so a store that
    was never relabelled still produces the right answer.
    """
    if antecedent.get("retracted"):
        return Boundary.OUT
    label = antecedent.get("label")
    if label == "OUT":
        return Boundary.OUT
    if label == "UNKNOWN":
        return Boundary.UNKNOWN
    if label == "IN":
        return Boundary.IN
    return None


def pull_projection(actor, scope_ids: Iterable[str]) -> Projection:
    """Read the justification layer for `scope_ids` and build a Projection."""
    scope = sorted(set(scope_ids))
    rows = actor.read(Q_PULL_PROJECTION, {"scope_ids": scope})

    justifications: list[Justification] = []
    boundary: dict[str, Boundary] = {}
    consequents: set[str] = set()

    for row in rows:
        antecedent_ids: list[str] = []
        for antecedent in row["antecedents"]:
            bid = antecedent["id"]
            antecedent_ids.append(bid)
            fixed = _boundary_from_row(antecedent)
            if fixed is not None:
                # OUT wins over IN: a retracted belief stays out even if some
                # other row still carries a stale IN label for it.
                if boundary.get(bid) is not Boundary.OUT:
                    boundary[bid] = fixed
        consequents.add(row["consequent"])
        justifications.append(
            Justification(
                id=row["jid"],
                consequent=row["consequent"],
                antecedents=tuple(antecedent_ids),
                threshold=row["threshold"],
                rule=row.get("rule") or "",
                confidence=float(row.get("confidence") or 1.0),
                provenance_tier=row.get("tier") or "curated",
            )
        )

    # A derived claim must never be seeded as a premise from its stored label,
    # or it would hold itself up and every retraction would be a no-op.
    for consequent in consequents:
        boundary.pop(consequent, None)

    return Projection(justifications, boundary=boundary, scope=scope)


def prior_support_counts(projection: Projection) -> dict[str, int]:
    """Support each belief had before the retraction, for weakening detection.

    Computed with every non-retracted antecedent treated as live, which is the
    state the graph was in before the change.
    """
    pre = Projection(
        projection.justifications,
        boundary={
            bid: (Boundary.IN if lbl is Boundary.OUT else lbl)
            for bid, lbl in (
                (projection.ids[i], lbl) for i, lbl in projection.boundary.items()
            )
        },
        scope=[projection.ids[i] for i in sorted(projection.scope)],
    )
    label = pre.lfp(unknown_is_in=False)
    return {
        pre.ids[b]: len(pre.live_support(b, label))
        for b in range(len(pre.ids))
    }


def propagate(
    actor,
    *,
    retracted_work: str | None = None,
    epoch: int = 1,
    questioned: Iterable[str] = (),
    write_back: bool = True,
) -> PropagationResult:
    """Retract a work if given, relabel everything downstream, persist."""
    if retracted_work:
        actor.write(Q_MARK_RETRACTED_OUT, {"work_id": retracted_work})

    scope = [row["id"] for row in actor.read(Q_ALL_CLAIM_IDS)]
    projection = pull_projection(actor, scope)
    prior = prior_support_counts(projection)

    verdicts = classify(
        projection,
        prior_support=prior,
        questioned=questioned,
        retracted=[retracted_work] if retracted_work else [],
    )

    summary: dict[str, int] = {}
    for verdict in verdicts:
        summary[verdict.status.value] = summary.get(verdict.status.value, 0) + 1

    if write_back:
        rows = [
            {
                "claim_id": v.belief_id,
                "label": label_for(v.status),
                "status": v.status.value,
                "reason_code": v.reason_code,
                "epoch": epoch,
            }
            for v in verdicts
        ]
        if rows:
            actor.write(Q_WRITEBACK_STATUS, {"rows": rows})

    return PropagationResult(
        epoch=epoch,
        retracted_work=retracted_work,
        verdicts=verdicts,
        summary=summary,
        n_justifications=len(projection.justifications),
        n_beliefs=len(projection),
    )


def retraction_event(actor, work_id: str) -> dict[str, Any] | None:
    rows = actor.read(Q_RETRACTION_EVENT, {"work_id": work_id})
    return rows[0] if rows else None


def post_retraction_breakdown(actor, work_id: str) -> list[dict[str, Any]]:
    return actor.read(Q_POST_RETRACTION_BREAKDOWN, {"work_id": work_id})
