"""Justification-based truth maintenance over a projected sub-hypergraph.

What this is for
----------------
Relate's own cookbook can already record that a belief changed and query what
was believed when. What nothing there does is recompute what still holds
afterwards. That is this module.

The governing rule is that a citation is not a justification. Running a TMS
over CITES edges would mean "connected to a retracted paper = broken", which
is both wrong and the exact failure this project exists to avoid. Status is
computed only over the justification layer, and graph distance is never an
input to it.

Representation
--------------
A Justification is a reified hyperedge: a set of antecedent beliefs, one
consequent belief, and a threshold t with 1 <= t <= arity.

`threshold` subsumes AND and OR, so the hot loop has one rule and no branching:

  * AND is intra-justification: threshold == arity, every antecedent required.
  * OR is inter-justification: a belief holds if ANY of its justifications is
    satisfied. That is Doyle's JTMS and needs no separate representation, so
    authored OR-groups are normalized into k singleton justifications on load.
  * threshold == 2 over 5 antecedents encodes "needs two independent
    replications", for the price of one integer. This is what stops false
    propagation: a 3-of-7 conclusion does not die when one antecedent is
    retracted, and the engine can say so with the surviving witness.

Works and Claims are both beliefs. A work with no justification is a premise,
so a retraction is simply a premise flipped OUT and there is one code path.

Algorithm
---------
Counter-based least fixpoint, i.e. Dowling-Gallier (1984) unit propagation.
Each justification holds a countdown initialized to its threshold; deriving an
antecedent decrements it; reaching zero derives the consequent. Every belief
is dequeued at most once, so the pass is O(V + E).

This is cycle-immune by construction, which matters because reviews cite
reviews and the citation graph is full of cycles. A least fixpoint cannot be
satisfied by a cyclic derivation: nothing inside a cycle ever reaches counter
zero without an external grounding. There is no cycle detection here because
none is needed, and the answer is correct rather than merely terminating.

Three fixpoints are run, and the status lattice falls out of the gaps:

  lfp_pess  uningested antecedents treated OUT. Conservative and grounded.
  lfp_opt   uningested antecedents treated IN.
  gfp       assume everything in scope holds, then erode until stable.

lfp subset-of gfp always. The pess/opt gap is exactly open-world uncertainty;
the lfp/gfp gap is exactly circular support. Both are things a reviewer wants
flagged rather than silently resolved.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence


class Status(str, Enum):
    """What RIPPLE is entitled to say about a belief after a change.

    Connectivity appears nowhere in these definitions. Hop distance is
    reported alongside a status but is never an input to it.
    """

    SUPPORTED = "SUPPORTED"
    SUPPORTED_WEAKENED = "SUPPORTED_WEAKENED"
    INDETERMINATE = "INDETERMINATE"
    UNSUPPORTED_CIRCULAR = "UNSUPPORTED_CIRCULAR"
    UNSUPPORTED = "UNSUPPORTED"
    QUESTIONED = "QUESTIONED"
    RETRACTED = "RETRACTED"
    UNAFFECTED = "UNAFFECTED"


class Boundary(str, Enum):
    """A label fixed from outside the projection rather than derived in it."""

    IN = "IN"
    OUT = "OUT"
    UNKNOWN = "UNKNOWN"


class ReasonClass(str, Enum):
    """Why a work was retracted.

    Roughly a third of real retractions are procedural (plagiarism, authorship
    disputes, duplicate publication) and do not impeach the findings. Treating
    those as "the result is false" is wrong about the world and is the easiest
    thing for a domain expert to attack, so PROCEDURAL leaves the premise IN
    and only flags dependents.
    """

    SUBSTANTIVE = "SUBSTANTIVE"
    PROCEDURAL = "PROCEDURAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Justification:
    """One reified hyperedge: antecedents -> consequent, satisfied at threshold."""

    id: str
    consequent: str
    antecedents: tuple[str, ...]
    # None means "unset, so default to AND". An explicit 0 is a mistake in the
    # curated data and must not be silently read as AND, so it is rejected.
    threshold: int | None = None
    rule: str = ""
    confidence: float = 1.0
    provenance_tier: str = "curated"

    def __post_init__(self) -> None:
        if not self.antecedents:
            raise ValueError(f"justification {self.id} has no antecedents")
        if len(set(self.antecedents)) != len(self.antecedents):
            raise ValueError(f"justification {self.id} has duplicate antecedents")
        t = len(self.antecedents) if self.threshold is None else self.threshold
        if not 1 <= t <= len(self.antecedents):
            raise ValueError(
                f"justification {self.id}: threshold {t} outside 1..{len(self.antecedents)}"
            )
        object.__setattr__(self, "threshold", t)

    @property
    def arity(self) -> int:
        return len(self.antecedents)


@dataclass
class Verdict:
    """The engine's finding for one belief, with the evidence behind it."""

    belief_id: str
    status: Status
    reason_code: str
    witness_justification_id: str | None = None
    witness_chain: list[str] = field(default_factory=list)
    killers: list[str] = field(default_factory=list)
    missing_antecedents: list[str] = field(default_factory=list)
    live_support_count: int = 0
    prior_support_count: int = 0


class Projection:
    """A sub-hypergraph pulled from Relate once and labelled in memory.

    Cypher has no recursive fixpoint and no WHILE, so the engine never loops a
    query. It pulls the affected region in one read and iterates here. The
    alternative, a single variable-length query returning paths, is exponential
    over a cyclic citation graph and the engine's hop cap does not save it.

    Beliefs are mapped to dense integers so every inner loop is list indexing
    and iteration order is deterministic, which the golden-certificate test
    depends on.
    """

    def __init__(
        self,
        justifications: Sequence[Justification],
        *,
        boundary: dict[str, Boundary] | None = None,
        scope: Iterable[str] | None = None,
    ) -> None:
        self.justifications = list(justifications)
        self._boundary_in = dict(boundary or {})

        ids: list[str] = []
        seen: set[str] = set()
        for j in self.justifications:
            for b in (*j.antecedents, j.consequent):
                if b not in seen:
                    seen.add(b)
                    ids.append(b)
        for b in self._boundary_in:
            if b not in seen:
                seen.add(b)
                ids.append(b)
        ids.sort()  # determinism: the certificate hash depends on it

        self.ids: list[str] = ids
        self.idx: dict[str, int] = {b: i for i, b in enumerate(ids)}
        n = len(ids)

        self.ant: list[list[int]] = []
        self.cons: list[int] = []
        self.thresh: list[int] = []
        self.just_of: list[list[int]] = [[] for _ in range(n)]
        self.justs_for: list[list[int]] = [[] for _ in range(n)]

        for jpos, j in enumerate(self.justifications):
            a = [self.idx[x] for x in j.antecedents]
            c = self.idx[j.consequent]
            self.ant.append(a)
            self.cons.append(c)
            self.thresh.append(j.threshold)
            for x in a:
                self.just_of[x].append(jpos)
            self.justs_for[c].append(jpos)

        self.boundary: dict[int, Boundary] = {
            self.idx[b]: lbl for b, lbl in self._boundary_in.items() if b in self.idx
        }
        # A belief nothing in scope derives is a premise: it is asserted, not
        # concluded. Retraction is exactly "a premise flipped OUT".
        self.premise = bytearray(1 if not self.justs_for[i] else 0 for i in range(n))
        self.scope: set[int] = (
            {self.idx[b] for b in scope if b in self.idx} if scope is not None else set(range(n))
        )
        self.witness: list[int | None] = [None] * n

    def __len__(self) -> int:
        return len(self.ids)

    def _seeds(self, unknown_is_in: bool) -> list[int]:
        seeds = []
        for b in range(len(self.ids)):
            fixed = self.boundary.get(b)
            if fixed is Boundary.OUT:
                continue
            if fixed is Boundary.IN:
                seeds.append(b)
            elif fixed is Boundary.UNKNOWN:
                if unknown_is_in:
                    seeds.append(b)
            elif self.premise[b]:
                seeds.append(b)
        return seeds

    def lfp(self, *, unknown_is_in: bool, record_witness: bool = False) -> bytearray:
        """Least fixpoint by counter propagation. O(V + E), cycle-immune."""
        n = len(self.ids)
        label = bytearray(n)
        counter = list(self.thresh)
        queue: deque[int] = deque()

        for b in self._seeds(unknown_is_in):
            if not label[b]:
                label[b] = 1
                queue.append(b)

        while queue:
            b = queue.popleft()
            for j in self.just_of[b]:
                counter[j] -= 1
                if counter[j] != 0:
                    continue
                c = self.cons[j]
                if self.boundary.get(c) is Boundary.OUT:
                    continue  # an externally refuted belief is never re-derived
                if not label[c]:
                    label[c] = 1
                    if record_witness:
                        self.witness[c] = j
                    queue.append(c)
        return label

    def gfp(self) -> bytearray:
        """Greatest fixpoint: assume everything holds, then erode.

        What survives here but not in any lfp is supported only by a cycle,
        which is the unfounded set and is reported rather than believed.
        """
        n = len(self.ids)
        alive = bytearray([1]) * n
        for b, lbl in self.boundary.items():
            if lbl is Boundary.OUT:
                alive[b] = 0

        changed = True
        while changed:
            changed = False
            for b in range(n):
                if not alive[b] or self.premise[b]:
                    continue
                ok = any(
                    sum(alive[a] for a in self.ant[j]) >= self.thresh[j]
                    for j in self.justs_for[b]
                )
                if not ok:
                    alive[b] = 0
                    changed = True
        return alive

    def witness_chain(self, belief: int, label: bytearray, _depth: int = 0) -> list[str]:
        """Walk the recorded derivation back to premises.

        Safe to recurse: the pointers were recorded during a least fixpoint, so
        the chain they describe is grounded and acyclic by construction.
        """
        if _depth > 64:
            return []
        j = self.witness[belief]
        if j is None:
            return [self.ids[belief]]
        chain: list[str] = []
        for a in self.ant[j]:
            if label[a]:
                chain.extend(self.witness_chain(a, label, _depth + 1))
        chain.append(self.ids[belief])
        return chain

    def live_support(self, belief: int, label: bytearray) -> list[int]:
        """Justifications currently meeting their threshold on live antecedents."""
        return [
            j
            for j in self.justs_for[belief]
            if sum(label[a] for a in self.ant[j]) >= self.thresh[j]
        ]

    def killers(self, belief: int, label: bytearray) -> list[int]:
        """Justifications that exist but fall short, i.e. why support was lost."""
        return [
            j
            for j in self.justs_for[belief]
            if sum(label[a] for a in self.ant[j]) < self.thresh[j]
        ]


def classify(
    projection: Projection,
    *,
    prior_support: dict[str, int] | None = None,
    questioned: Iterable[str] = (),
    retracted: Iterable[str] = (),
) -> list[Verdict]:
    """Run all three fixpoints and derive the status lattice from the gaps."""
    p = projection
    prior = prior_support or {}
    questioned_set = set(questioned)
    retracted_set = set(retracted)

    lfp_pess = p.lfp(unknown_is_in=False, record_witness=True)
    lfp_opt = p.lfp(unknown_is_in=True)
    greatest = p.gfp()

    verdicts: list[Verdict] = []
    for b in sorted(p.scope):
        bid = p.ids[b]

        if bid in retracted_set:
            verdicts.append(Verdict(bid, Status.RETRACTED, "RETRACTED_PREMISE"))
            continue

        live = p.live_support(b, lfp_pess)
        n_live = len(live)
        n_prior = prior.get(bid, n_live)

        if lfp_pess[b]:
            if bid in questioned_set:
                status, reason = Status.QUESTIONED, "PROCEDURAL_RETRACTION_UPSTREAM"
            elif n_live < n_prior:
                status, reason = Status.SUPPORTED_WEAKENED, "LOST_INDEPENDENT_SUPPORT"
            else:
                status, reason = Status.SUPPORTED, "WELL_FOUNDED_SUPPORT"
            witness = p.witness[b]
            verdicts.append(
                Verdict(
                    belief_id=bid,
                    status=status,
                    reason_code=reason,
                    witness_justification_id=(
                        p.justifications[witness].id if witness is not None else None
                    ),
                    witness_chain=p.witness_chain(b, lfp_pess),
                    live_support_count=n_live,
                    prior_support_count=n_prior,
                )
            )
            continue

        if lfp_opt[b]:
            missing = sorted(
                {
                    p.ids[a]
                    for j in p.justs_for[b]
                    for a in p.ant[j]
                    if p.boundary.get(a) is Boundary.UNKNOWN
                }
            )
            verdicts.append(
                Verdict(bid, Status.INDETERMINATE, "UNINGESTED_ANTECEDENTS",
                        missing_antecedents=missing, prior_support_count=n_prior)
            )
            continue

        killers = [p.justifications[j].id for j in p.killers(b, lfp_pess)]
        if greatest[b]:
            verdicts.append(
                Verdict(bid, Status.UNSUPPORTED_CIRCULAR, "SUPPORT_ONLY_IN_UNFOUNDED_SET",
                        killers=killers, prior_support_count=n_prior)
            )
        else:
            verdicts.append(
                Verdict(bid, Status.UNSUPPORTED, "ALL_JUSTIFICATIONS_BELOW_THRESHOLD",
                        killers=killers, prior_support_count=n_prior)
            )
    return verdicts
