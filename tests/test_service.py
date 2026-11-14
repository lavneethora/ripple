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
SYNTHETIC_FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_lattice.yaml"
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
    """Verdicts over the curated claims once the retraction is known."""
    return propagate(actor, retracted_work=FOCAL, epoch=2)


@pytest.fixture()
def lattice(actor: GraphActor):
    """The synthetic fixture, loaded into a throwaway copy.

    It covers branches the real evidence base does not contain: circular
    support, an unadjudicated antecedent, and a long derivation chain. It is
    never written to the committed store.
    """
    from ripple.ingest import justifications

    justifications.load(actor, SYNTHETIC_FIXTURE)
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


def test_guidance_retraction_facts_are_classified_not_counted(actor: GraphActor):
    """OpenAlex flags three of this document's references as retracted.

    Only one of those holds up as a substantive retraction. A second was
    withdrawn as an accidental duplicate, which says nothing about whether
    its findings are correct. The third has no retraction record in Crossref
    at all and appears to have inherited the flag from the duplicate.

    Reporting "cites three retracted works" would therefore overstate it by
    a factor of three, which is exactly the failure mode this project is
    built to catch, so the breakdown is pinned rather than the headline.
    """
    retracted = actor.read(
        "MATCH (d:Work {openalex_id: $d})-[:CITES]->(ref:Work) "
        "WHERE ref.is_retracted = true RETURN count(ref) AS n",
        {"d": ESC_GUIDANCE},
    )[0]["n"]
    substantive = actor.read(
        "MATCH (d:Work {openalex_id: $d})-[:CITES]->(ref:Work)"
        "-[:HAS_RETRACTION]->(re:RetractionEvent) "
        "WHERE re.reason_class = 'SUBSTANTIVE' RETURN count(ref) AS n",
        {"d": ESC_GUIDANCE},
    )[0]["n"]
    disputed = actor.read(
        "MATCH (d:Work {openalex_id: $d})-[:CITES]->(ref:Work) "
        "WHERE ref.openalex_flag_disputed = true RETURN count(ref) AS n",
        {"d": ESC_GUIDANCE},
    )[0]["n"]

    assert retracted == 2, "two hold up as retractions"
    assert substantive == 1, "only one impeaches its findings"
    assert disputed == 1, "one OpenAlex flag has no Crossref record behind it"


def test_openalex_false_positive_is_not_treated_as_retracted(actor: GraphActor):
    row = actor.read(
        "MATCH (w:Work {openalex_id: 'W3025220694'}) "
        "RETURN w.is_retracted AS retracted, w.openalex_flag_disputed AS disputed"
    )[0]
    assert row["retracted"] is False
    assert row["disputed"] is True


def test_procedural_withdrawal_is_recorded_as_such(actor: GraphActor):
    row = actor.read(
        "MATCH (:Work {openalex_id: 'W4205843467'})-[:HAS_RETRACTION]->(re:RetractionEvent) "
        "RETURN re.reason_class AS reason, re.retracted_on AS on"
    )[0]
    assert row["reason"] == "PROCEDURAL"
    assert row["on"] == "2020-05-27"


def test_exposure_is_reported_separately_from_loss_of_support(actor: GraphActor):
    """Citation reach is large and is not a claim about support."""
    breakdown = {r["awareness"]: r["n"] for r in post_retraction_breakdown(actor, FOCAL)}
    assert breakdown["unambiguous"] == 6
    # Conservative buckets must stay out of the headline figure.
    assert breakdown["plausible_pipeline"] > breakdown["unambiguous"]
    assert "unknown_precision" in breakdown


# --- the engine's verdicts ---

def test_sole_support_on_a_retracted_study_collapses(lattice):
    assert lattice.status_of("SYN_SOLE_SUPPORT") is Status.UNSUPPORTED


def test_corroborated_claim_one_hop_away_survives(lattice):
    assert lattice.status_of("SYN_NEAR_CORROBORATED") is Status.SUPPORTED


def test_wholly_dependent_claim_five_hops_away_collapses(lattice):
    assert lattice.status_of("SYN_FAR_DEPENDENT") is Status.UNSUPPORTED


def test_status_does_not_track_graph_distance(lattice):
    """The demo's central claim, asserted against the real store."""
    near = lattice.status_of("SYN_NEAR_CORROBORATED")
    far = lattice.status_of("SYN_FAR_DEPENDENT")
    assert near is Status.SUPPORTED and far is Status.UNSUPPORTED


def test_losing_one_of_two_supports_weakens_rather_than_breaks(lattice):
    assert lattice.status_of("SYN_WEAKENED") is Status.SUPPORTED_WEAKENED


def test_unrelated_evidence_is_untouched(lattice):
    assert lattice.status_of("SYN_UNRELATED") is Status.SUPPORTED


def test_mutually_citing_ring_is_circular(lattice):
    for ring in ("SYN_RING_A", "SYN_RING_B", "SYN_RING_C"):
        assert lattice.status_of(ring) is Status.UNSUPPORTED_CIRCULAR


def test_unadjudicated_antecedent_is_not_guessed(lattice):
    assert lattice.status_of("SYN_DEPENDS_ON_UNKNOWN") is Status.INDETERMINATE


# --- before and after ---

def test_beliefs_actually_change_when_the_retraction_is_learned(actor: GraphActor):
    from ripple.ingest import justifications

    justifications.load(actor, SYNTHETIC_FIXTURE)
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


def test_verdicts_are_persisted_to_the_store(actor: GraphActor, after):  # noqa: ARG001
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
    from ripple.ingest import justifications

    justifications.load(actor, SYNTHETIC_FIXTURE)
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


def test_committed_store_carries_only_curated_justifications(actor: GraphActor):
    """No synthetic structure may ship in the artifact judges inspect."""
    rows = actor.read(
        "MATCH (j:Justification) RETURN j.provenance_tier AS tier, count(j) AS n"
    )
    tiers = {r["tier"]: r["n"] for r in rows}
    assert tiers, "the committed store has no justification layer"
    assert set(tiers) == {"curated"}, f"non-curated structure in the store: {tiers}"


# --- label round-tripping ---

def test_indeterminate_round_trips_as_unknown(actor: GraphActor):
    from ripple.ingest import justifications

    justifications.load(actor, SYNTHETIC_FIXTURE)
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


# --- the curated evidence base ---

def test_curated_claim_resting_on_the_retracted_study_collapses(after):
    """The retraction destroys the claim that hydroxychloroquine works."""
    assert after.status_of("CL_HCQ_TREATS_COVID") is Status.UNSUPPORTED


def test_the_recommendation_against_hcq_is_untouched(after):
    """The substantive result, and the reason this is worth building.

    "Hydroxychloroquine should not be used" never rested on the retracted
    study. It rests on three independent randomised trials that found no
    benefit, so retracting the paper that claimed benefit does not disturb
    it. A system that propagated by citation distance would wrongly flag it.
    """
    assert after.status_of("CL_HCQ_NOT_INDICATED") is Status.SUPPORTED
    assert after.status_of("CL_HCQ_NO_MORTALITY_BENEFIT") is Status.SUPPORTED
    assert after.status_of("CL_HCQ_NO_CLINICAL_BENEFIT") is Status.SUPPORTED


def test_two_of_three_real_trials_carry_the_null_result(actor: GraphActor, after):  # noqa: ARG001
    row = actor.read(
        "MATCH (j:Justification {id: 'J_HCQ_NO_MORTALITY_RCTS'}) "
        "RETURN j.threshold AS threshold, j.arity AS arity"
    )[0]
    assert (row["threshold"], row["arity"]) == (2, 3)


def test_procedural_withdrawal_questions_rather_than_breaks(after):
    """Both references here are flagged retracted, and neither flag holds.

    One has no Crossref retraction record at all; the other was withdrawn as
    an accidental duplicate of it. Breaking the claim would be wrong twice
    over, so it is flagged for review instead.
    """
    assert after.status_of("CL_COVID_HEART_BLOCK_CHILD") is Status.QUESTIONED


def test_unrelated_curated_claim_is_untouched(after):
    assert after.status_of("CL_DEXAMETHASONE_REDUCES_MORTALITY") is Status.SUPPORTED


def test_only_one_curated_claim_loses_support(after):
    assert after.summary == {"SUPPORTED": 4, "QUESTIONED": 1, "UNSUPPORTED": 1}


def test_claims_and_works_carry_readable_labels(actor: GraphActor):
    """The canvas shows these, so a missing one degrades to a raw id."""
    claims = actor.read("MATCH (c:Claim) RETURN c.id AS id, c.short AS short")
    for row in claims:
        assert row["short"], f"{row['id']} has no short label"
        assert row["short"] != row["id"], f"{row['id']} fell back to its id"

    labelled = actor.read(
        "MATCH (w:Work)-[:ANTECEDENT_OF]->(:Justification) "
        "RETURN DISTINCT w.openalex_id AS id, w.short AS short"
    )
    assert labelled, "no works feed any justification"
    for row in labelled:
        assert row["short"], f"{row['id']} has no short label"
