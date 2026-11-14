"""The Changing-Mind Test, run against the committed store in CI.

These assertions are the project's substantive claims. If one of them stops
holding, the demo is making a statement the engine cannot back.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ripple.graph.actor import GraphActor
from ripple.tms.engine import Status
from ripple.tms.service import (
    post_retraction_breakdown,
    propagate,
    pull_projection,
    retraction_event,
)

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "data" / "ripple-retractions"
FOCAL = "W3010930696"          # Gautret 2020, retracted 2020-07-01
ESC_GUIDANCE = "W3216235612"   # cites it 439 days later


@pytest.fixture()
def actor(tmp_path: Path):
    """A throwaway copy of the committed store.

    Propagation writes, and Relate rewrites snapshot files on every write, so
    running against the committed artifact would leave it dirty after a test.
    """
    target = tmp_path / "store"
    shutil.copytree(STORE, target)
    a = GraphActor(target, readers=1)
    yield a
    a.close()


@pytest.fixture()
def after(actor: GraphActor):
    return propagate(actor, retracted_work=FOCAL, epoch=2)


# --- the store is what it claims to be ---

def test_committed_store_has_the_real_retraction(actor: GraphActor):
    event = retraction_event(actor, FOCAL)
    assert event is not None
    assert event["retracted_on"] == "2020-07-01"
    assert event["source"] == "publisher"
    assert event["doi"] == "10.1016/j.ijantimicag.2020.105949"


def test_esc_guidance_cites_it_439_days_after_retraction(actor: GraphActor):
    row = actor.read(
        "MATCH (d:Work {openalex_id: $d})-[r:CITES]->(:Work {openalex_id: $f}) "
        "RETURN r.post_retraction_lag_days AS lag, r.awareness AS awareness",
        {"d": ESC_GUIDANCE, "f": FOCAL},
    )[0]
    assert row["lag"] == 439
    assert row["awareness"] == "unambiguous"


def test_guidance_cites_more_than_one_retracted_work(actor: GraphActor):
    n = actor.read(
        "MATCH (d:Work {openalex_id: $d})-[:CITES]->(ref:Work) "
        "WHERE ref.is_retracted = true RETURN count(ref) AS n",
        {"d": ESC_GUIDANCE},
    )[0]["n"]
    assert n == 3


def test_exposure_is_reported_separately_from_loss_of_support(actor: GraphActor):
    """Citation reach is large and is not a claim about support."""
    breakdown = {r["awareness"]: r["n"] for r in post_retraction_breakdown(actor, FOCAL)}
    assert breakdown["unambiguous"] == 6
    # Conservative buckets must stay out of the headline figure.
    assert breakdown["plausible_pipeline"] > breakdown["unambiguous"]
    assert "unknown_precision" in breakdown


# --- the engine's verdicts ---

def test_sole_support_on_a_retracted_study_collapses(after):
    assert after.status_of("SYN_SOLE_SUPPORT") is Status.UNSUPPORTED


def test_corroborated_claim_one_hop_away_survives(after):
    assert after.status_of("SYN_NEAR_CORROBORATED") is Status.SUPPORTED


def test_wholly_dependent_claim_five_hops_away_collapses(after):
    assert after.status_of("SYN_FAR_DEPENDENT") is Status.UNSUPPORTED


def test_status_does_not_track_graph_distance(after):
    """The demo's central claim, asserted against the real store."""
    near = after.status_of("SYN_NEAR_CORROBORATED")
    far = after.status_of("SYN_FAR_DEPENDENT")
    assert near is Status.SUPPORTED and far is Status.UNSUPPORTED


def test_losing_one_of_two_supports_weakens_rather_than_breaks(after):
    assert after.status_of("SYN_WEAKENED") is Status.SUPPORTED_WEAKENED


def test_unrelated_evidence_is_untouched(after):
    assert after.status_of("SYN_UNRELATED") is Status.SUPPORTED


def test_mutually_citing_ring_is_circular(after):
    for ring in ("SYN_RING_A", "SYN_RING_B", "SYN_RING_C"):
        assert after.status_of(ring) is Status.UNSUPPORTED_CIRCULAR


def test_unadjudicated_antecedent_is_not_guessed(after):
    assert after.status_of("SYN_DEPENDS_ON_UNKNOWN") is Status.INDETERMINATE


# --- before and after ---

def test_beliefs_actually_change_when_the_retraction_is_learned(actor: GraphActor):
    actor.write(
        "MATCH (w:Work {openalex_id:$id}) SET w.is_retracted = false, w.label = 'IN'",
        {"id": FOCAL},
    )
    before = propagate(actor, epoch=1, write_back=False)
    assert before.status_of("SYN_SOLE_SUPPORT") is Status.SUPPORTED
    assert before.status_of("SYN_FAR_DEPENDENT") is Status.SUPPORTED

    after = propagate(actor, retracted_work=FOCAL, epoch=2)
    assert after.status_of("SYN_SOLE_SUPPORT") is Status.UNSUPPORTED
    assert after.status_of("SYN_FAR_DEPENDENT") is Status.UNSUPPORTED
    assert before.summary != after.summary


def test_verdicts_are_persisted_to_the_store(actor: GraphActor, after):
    rows = actor.read(
        "MATCH (c:Claim) WHERE c.status IS NOT NULL "
        "RETURN c.status AS status, count(c) AS n ORDER BY status"
    )
    persisted = {r["status"]: r["n"] for r in rows}
    assert persisted == after.summary


def test_propagation_is_deterministic(actor: GraphActor):
    first = propagate(actor, retracted_work=FOCAL, epoch=2, write_back=False)
    for _ in range(3):
        again = propagate(actor, retracted_work=FOCAL, epoch=2, write_back=False)
        assert again.summary == first.summary
        assert [(v.belief_id, v.status) for v in again.verdicts] == [
            (v.belief_id, v.status) for v in first.verdicts
        ]


# --- projection integrity ---

def test_derived_claims_are_never_seeded_as_premises(actor: GraphActor):
    """A derived claim seeded from its stored label would hold itself up.

    If that happened every retraction would be a silent no-op, so the guard
    is asserted directly rather than inferred from the verdicts.
    """
    scope = [r["id"] for r in actor.read("MATCH (c:Claim) RETURN c.id AS id")]
    projection = pull_projection(actor, scope)
    for justification in projection.justifications:
        index = projection.idx[justification.consequent]
        assert not projection.premise[index], (
            f"{justification.consequent} has a justification but was treated as a premise"
        )


def test_retracted_work_is_out_regardless_of_stale_label(actor: GraphActor):
    """A store that was never relabelled must still give the right answer."""
    actor.write(
        "MATCH (w:Work {openalex_id:$id}) SET w.is_retracted = true, w.label = 'IN'",
        {"id": FOCAL},
    )
    scope = [r["id"] for r in actor.read("MATCH (c:Claim) RETURN c.id AS id")]
    projection = pull_projection(actor, scope)
    from ripple.tms.engine import Boundary

    assert projection.boundary[projection.idx[FOCAL]] is Boundary.OUT


def test_synthetic_fixtures_are_tagged_as_such(actor: GraphActor):
    """Nothing in the demo fixture may pass as curated evidence."""
    rows = actor.read(
        "MATCH (j:Justification) RETURN j.provenance_tier AS tier, count(j) AS n"
    )
    tiers = {r["tier"]: r["n"] for r in rows}
    assert set(tiers) == {"synthetic"}, (
        "the fixture must stay tagged synthetic until real curation replaces it"
    )


# --- label round-tripping ---

def test_indeterminate_round_trips_as_unknown(actor: GraphActor):
    """Repeated propagation must not quietly erase 'we cannot tell'.

    The stored label feeds the next propagation, so collapsing INDETERMINATE
    to OUT is permanent: the belief is treated as refuted from then on and
    the status can never reappear.
    """
    first = propagate(actor, retracted_work=FOCAL, epoch=2)
    assert first.status_of("SYN_DEPENDS_ON_UNKNOWN") is Status.INDETERMINATE

    label = actor.read(
        "MATCH (c:Claim {id:'SYN_NOT_ADJUDICATED'}) RETURN c.label AS label"
    )[0]["label"]
    assert label == "UNKNOWN", "an unadjudicated belief was relabelled"

    second = propagate(actor, retracted_work=FOCAL, epoch=3)
    assert second.summary == first.summary


def test_repeated_propagation_is_stable(actor: GraphActor):
    """Running the engine N times must not drift the store's conclusions."""
    summaries = [propagate(actor, retracted_work=FOCAL, epoch=i).summary for i in range(2, 7)]
    assert all(s == summaries[0] for s in summaries), summaries
