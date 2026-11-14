"""Correctness tests for the truth maintenance engine.

Every claim RIPPLE makes on screen reduces to one of these behaviours, so
these are the tests that decide whether the project is defensible.
"""

from __future__ import annotations

import pytest

from ripple.tms.engine import (
    Boundary,
    Justification,
    Projection,
    Status,
    classify,
)


def statuses(verdicts) -> dict[str, Status]:
    return {v.belief_id: v.status for v in verdicts}


def verdict_for(verdicts, belief_id):
    return next(v for v in verdicts if v.belief_id == belief_id)


# --------------------------------------------------------------------------
# Retraction and propagation
# --------------------------------------------------------------------------

def test_retracted_premise_collapses_its_sole_dependent():
    """The base case: one study, one conclusion, study retracted."""
    p = Projection(
        [Justification("j1", "claim", ("study",))],
        boundary={"study": Boundary.OUT},
    )
    s = statuses(classify(p, retracted=["study"]))
    assert s["study"] is Status.RETRACTED
    assert s["claim"] is Status.UNSUPPORTED


def test_unrelated_belief_is_untouched():
    """A conclusion with no dependency path to the change must not move."""
    p = Projection(
        [
            Justification("j1", "claim_a", ("study_x",)),
            Justification("j2", "claim_b", ("study_y",)),
        ],
        boundary={"study_x": Boundary.OUT, "study_y": Boundary.IN},
    )
    s = statuses(classify(p, retracted=["study_x"]))
    assert s["claim_a"] is Status.UNSUPPORTED
    assert s["claim_b"] is Status.SUPPORTED


def test_independent_corroboration_survives_a_retraction():
    """OR across justifications: losing one support is not losing support.

    This is the case that separates RIPPLE from a graph that colours
    everything downstream of a retracted node.
    """
    p = Projection(
        [
            Justification("j_retracted", "claim", ("bad_study",)),
            Justification("j_independent", "claim", ("good_study",)),
        ],
        boundary={"bad_study": Boundary.OUT, "good_study": Boundary.IN},
    )
    v = verdict_for(classify(p, prior_support={"claim": 2}, retracted=["bad_study"]), "claim")
    assert v.status is Status.SUPPORTED_WEAKENED
    assert v.reason_code == "LOST_INDEPENDENT_SUPPORT"
    assert v.live_support_count == 1
    assert v.prior_support_count == 2
    assert v.witness_justification_id == "j_independent"


def test_and_justification_fails_when_one_antecedent_dies():
    p = Projection(
        [Justification("j1", "claim", ("a", "b", "c"))],  # threshold defaults to arity
        boundary={"a": Boundary.IN, "b": Boundary.IN, "c": Boundary.OUT},
    )
    v = verdict_for(classify(p, retracted=["c"]), "claim")
    assert v.status is Status.UNSUPPORTED
    assert v.killers == ["j1"]


# --------------------------------------------------------------------------
# m-of-n corroboration
# --------------------------------------------------------------------------

def test_three_of_seven_survives_one_retraction():
    """'Needs two independent replications' must not die on one loss."""
    antecedents = tuple(f"s{i}" for i in range(7))
    p = Projection(
        [Justification("j1", "claim", antecedents, threshold=3)],
        boundary={"s0": Boundary.OUT, **{f"s{i}": Boundary.IN for i in range(1, 7)}},
    )
    v = verdict_for(classify(p, retracted=["s0"]), "claim")
    assert v.status is Status.SUPPORTED


def test_three_of_seven_fails_once_support_drops_below_threshold():
    antecedents = tuple(f"s{i}" for i in range(7))
    boundary = {f"s{i}": Boundary.OUT for i in range(5)}
    boundary.update({f"s{i}": Boundary.IN for i in range(5, 7)})
    p = Projection([Justification("j1", "claim", antecedents, threshold=3)], boundary=boundary)
    v = verdict_for(classify(p), "claim")
    assert v.status is Status.UNSUPPORTED


@pytest.mark.parametrize("n_out,expected", [(0, Status.SUPPORTED), (4, Status.SUPPORTED),
                                            (5, Status.UNSUPPORTED), (7, Status.UNSUPPORTED)])
def test_threshold_boundary_is_exact(n_out, expected):
    antecedents = tuple(f"s{i}" for i in range(7))
    boundary = {f"s{i}": (Boundary.OUT if i < n_out else Boundary.IN) for i in range(7)}
    p = Projection([Justification("j1", "claim", antecedents, threshold=3)], boundary=boundary)
    assert verdict_for(classify(p), "claim").status is expected


# --------------------------------------------------------------------------
# Cycles. Reviews cite reviews, so this is the common case, not an edge case.
# --------------------------------------------------------------------------

def test_three_cycle_with_no_grounding_is_circular_not_supported():
    """Mutually citing reviews with no primary study underneath them.

    A naive walk either never terminates or concludes all three hold. The
    least fixpoint must refuse them, and the greatest fixpoint keeps them,
    so the gap between the two is what identifies the unfounded set.
    """
    p = Projection(
        [
            Justification("j_ab", "review_a", ("review_b",)),
            Justification("j_bc", "review_b", ("review_c",)),
            Justification("j_ca", "review_c", ("review_a",)),
        ]
    )
    s = statuses(classify(p))
    assert s["review_a"] is Status.UNSUPPORTED_CIRCULAR
    assert s["review_b"] is Status.UNSUPPORTED_CIRCULAR
    assert s["review_c"] is Status.UNSUPPORTED_CIRCULAR


def test_cycle_grounded_by_a_primary_study_is_supported():
    """The same cycle, with one real study attached, is legitimately supported."""
    p = Projection(
        [
            Justification("j_ab", "review_a", ("review_b",)),
            Justification("j_bc", "review_b", ("review_c",)),
            Justification("j_ca", "review_c", ("review_a",)),
            Justification("j_ground", "review_a", ("primary_study",)),
        ],
        boundary={"primary_study": Boundary.IN},
    )
    s = statuses(classify(p))
    assert s["review_a"] is Status.SUPPORTED
    assert s["review_b"] is Status.SUPPORTED
    assert s["review_c"] is Status.SUPPORTED


def test_cycle_loses_grounding_when_the_study_is_retracted():
    """Retracting the one real study must collapse the whole ring to circular."""
    p = Projection(
        [
            Justification("j_ab", "review_a", ("review_b",)),
            Justification("j_bc", "review_b", ("review_c",)),
            Justification("j_ca", "review_c", ("review_a",)),
            Justification("j_ground", "review_a", ("primary_study",)),
        ],
        boundary={"primary_study": Boundary.OUT},
    )
    s = statuses(classify(p, retracted=["primary_study"]))
    assert s["review_a"] is Status.UNSUPPORTED_CIRCULAR
    assert s["review_b"] is Status.UNSUPPORTED_CIRCULAR
    assert s["review_c"] is Status.UNSUPPORTED_CIRCULAR


def test_self_justifying_belief_is_circular():
    p = Projection([Justification("j_self", "claim", ("claim",))])
    assert verdict_for(classify(p), "claim").status is Status.UNSUPPORTED_CIRCULAR


def test_large_cycle_terminates():
    """Termination is structural, but assert it so a regression is loud."""
    n = 500
    js = [Justification(f"j{i}", f"c{i}", (f"c{(i + 1) % n}",)) for i in range(n)]
    s = statuses(classify(Projection(js)))
    assert all(v is Status.UNSUPPORTED_CIRCULAR for v in s.values())


# --------------------------------------------------------------------------
# The inversion. This is the demo's central claim, so it gets an assertion.
# --------------------------------------------------------------------------

def test_status_does_not_correlate_with_hop_distance():
    """A 1-hop claim can survive while a 5-hop claim dies.

    near_claim sits directly on the retracted work but holds 2-of-3 support.
    far_claim is five derivations away and every chain bottoms out in the
    retracted work. If status tracked graph distance, this test fails.
    """
    js = [
        # 1 hop from the retraction, but corroborated.
        Justification("j_near", "near_claim", ("retracted", "alt_1", "alt_2"), threshold=2),
        # A 5-link chain grounded only in the retracted work.
        Justification("j_f1", "far_1", ("retracted",)),
        Justification("j_f2", "far_2", ("far_1",)),
        Justification("j_f3", "far_3", ("far_2",)),
        Justification("j_f4", "far_4", ("far_3",)),
        Justification("j_f5", "far_claim", ("far_4",)),
    ]
    p = Projection(
        js,
        boundary={"retracted": Boundary.OUT, "alt_1": Boundary.IN, "alt_2": Boundary.IN},
    )
    s = statuses(classify(p, retracted=["retracted"]))
    assert s["near_claim"] is Status.SUPPORTED, "1 hop away but corroborated"
    assert s["far_claim"] is Status.UNSUPPORTED, "5 hops away but wholly dependent"


# --------------------------------------------------------------------------
# Open world and procedural retractions
# --------------------------------------------------------------------------

def test_uningested_antecedent_is_indeterminate_not_a_guess():
    p = Projection(
        [Justification("j1", "claim", ("known", "not_ingested"))],
        boundary={"known": Boundary.IN, "not_ingested": Boundary.UNKNOWN},
    )
    v = verdict_for(classify(p), "claim")
    assert v.status is Status.INDETERMINATE
    assert v.missing_antecedents == ["not_ingested"]


def test_procedural_retraction_questions_rather_than_breaks():
    """Plagiarism does not make the finding false, so do not say it does."""
    p = Projection(
        [Justification("j1", "claim", ("plagiarised_study",))],
        boundary={"plagiarised_study": Boundary.IN},
    )
    v = verdict_for(classify(p, questioned=["claim"]), "claim")
    assert v.status is Status.QUESTIONED
    assert v.reason_code == "PROCEDURAL_RETRACTION_UPSTREAM"


# --------------------------------------------------------------------------
# Proof output
# --------------------------------------------------------------------------

def test_witness_chain_is_grounded_and_ordered():
    p = Projection(
        [
            Justification("j1", "mid", ("base",)),
            Justification("j2", "top", ("mid",)),
        ],
        boundary={"base": Boundary.IN},
    )
    v = verdict_for(classify(p), "top")
    assert v.status is Status.SUPPORTED
    assert v.witness_chain == ["base", "mid", "top"]


def test_witness_names_the_justification_that_actually_fired():
    p = Projection(
        [
            Justification("j_dead", "claim", ("dead_study",)),
            Justification("j_live", "claim", ("live_study",)),
        ],
        boundary={"dead_study": Boundary.OUT, "live_study": Boundary.IN},
    )
    v = verdict_for(classify(p), "claim")
    assert v.witness_justification_id == "j_live"
    assert "live_study" in v.witness_chain
    assert "dead_study" not in v.witness_chain


def test_results_are_deterministic_across_runs():
    """The golden-certificate test depends on this."""
    def build():
        return Projection(
            [
                Justification("j1", "c", ("a", "b"), threshold=1),
                Justification("j2", "d", ("c",)),
            ],
            boundary={"a": Boundary.IN, "b": Boundary.OUT},
        )

    first = [(v.belief_id, v.status, tuple(v.witness_chain)) for v in classify(build())]
    for _ in range(5):
        assert [(v.belief_id, v.status, tuple(v.witness_chain)) for v in classify(build())] == first


# --------------------------------------------------------------------------
# Representation guards
# --------------------------------------------------------------------------

def test_threshold_defaults_to_and():
    assert Justification("j", "c", ("a", "b", "x")).threshold == 3


@pytest.mark.parametrize("bad", [0, 4, -1])
def test_threshold_outside_arity_is_rejected(bad):
    with pytest.raises(ValueError):
        Justification("j", "c", ("a", "b", "x"), threshold=bad)


def test_duplicate_antecedents_are_rejected():
    """A repeated antecedent would let one belief satisfy a threshold twice."""
    with pytest.raises(ValueError):
        Justification("j", "c", ("a", "a"), threshold=2)


def test_empty_antecedents_rejected():
    with pytest.raises(ValueError):
        Justification("j", "c", ())
