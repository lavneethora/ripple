"""Retraction status and reason classification."""

from __future__ import annotations

import pytest

from ripple.ingest.retractions import (
    RetractionFacts,
    classify_reason,
    facts_from_crossref,
)
from ripple.schema.model import ReasonClass

DUPLICATE_NOTICE = (
    "The Publisher regrets that this article is an accidental duplication of an "
    "article that has already been published. The duplicate article has "
    "therefore been withdrawn."
)


def test_duplicate_publication_is_procedural():
    """The real withdrawal notice from the seeded data."""
    reason, excerpt = classify_reason(DUPLICATE_NOTICE)
    assert reason is ReasonClass.PROCEDURAL
    assert excerpt and "duplicat" in excerpt.lower()


@pytest.mark.parametrize("text", [
    "Retracted due to plagiarism of a previously published work.",
    "Withdrawn over an authorship dispute between the listed contributors.",
    "This article was withdrawn by the publisher for an administrative error.",
])
def test_procedural_reasons(text):
    assert classify_reason(text)[0] is ReasonClass.PROCEDURAL


@pytest.mark.parametrize("text", [
    "The data were found to have been fabricated.",
    "Retracted: the results are unreliable and could not be reproduced.",
    "Concerns about image duplication in Figure 3 were substantiated.",
])
def test_substantive_reasons(text):
    assert classify_reason(text)[0] is ReasonClass.SUBSTANTIVE


def test_procedural_wins_over_an_incidental_error_mention():
    """A duplicate notice that happens to say 'error' is still procedural."""
    text = "Withdrawn: an accidental duplication caused by a submission error."
    assert classify_reason(text)[0] is ReasonClass.PROCEDURAL


@pytest.mark.parametrize("text", [None, "", "This article has been retracted."])
def test_unclassifiable_reasons_stay_unknown(text):
    """Guessing a reason nobody has read would be the overreach to avoid."""
    assert classify_reason(text)[0] is ReasonClass.UNKNOWN


def test_unknown_reason_does_not_impeach_findings():
    facts = RetractionFacts(is_retracted=True, reason_class=ReasonClass.UNKNOWN)
    assert not facts.impeaches_findings


def test_only_substantive_retractions_impeach_findings():
    assert RetractionFacts(
        is_retracted=True, reason_class=ReasonClass.SUBSTANTIVE
    ).impeaches_findings
    assert not RetractionFacts(
        is_retracted=True, reason_class=ReasonClass.PROCEDURAL
    ).impeaches_findings


# --- Crossref as the authority ---

def test_openalex_false_positive_is_corrected():
    """The real case: a work OpenAlex flags with no Crossref record.

    10.1016/j.jaccas.2020.05.023 is flagged retracted by OpenAlex and has no
    retraction record in Crossref. What was withdrawn was a duplicate of it
    at another DOI.
    """
    facts = facts_from_crossref({"title": ["A case report"]}, openalex_is_retracted=True)
    assert facts.is_retracted is False
    assert facts.openalex_disagrees is True


def test_crossref_retraction_is_taken_with_its_earliest_date():
    message = {
        "title": ["RETRACTED: Something"],
        "update-to": [
            {"type": "retraction", "DOI": "10.1/late",
             "updated": {"date-parts": [[2024, 12, 16]]}, "source": "retraction-watch"},
            {"type": "retraction", "DOI": "10.1/early",
             "updated": {"date-parts": [[2020, 7, 1]]}, "source": "publisher"},
        ],
    }
    facts = facts_from_crossref(message, openalex_is_retracted=True)
    assert facts.is_retracted is True
    assert facts.retracted_on == "2020-07-01"
    assert facts.notice_doi == "10.1/early"


def test_withdrawal_counts_as_a_retraction_record():
    message = {
        "title": ["WITHDRAWN: Duplicated article"],
        "update-to": [
            {"type": "withdrawal", "DOI": "10.1/x",
             "updated": {"date-parts": [[2020, 5, 27]]}, "source": "publisher"},
        ],
    }
    facts = facts_from_crossref(message, openalex_is_retracted=True, notice_text=DUPLICATE_NOTICE)
    assert facts.is_retracted is True
    assert facts.retracted_on == "2020-05-27"
    assert facts.reason_class is ReasonClass.PROCEDURAL
    assert not facts.impeaches_findings


def test_erratum_alone_is_not_a_retraction():
    message = {
        "title": ["A paper"],
        "updated-by": [
            {"type": "erratum", "DOI": "10.1/e",
             "updated": {"date-parts": [[2021, 1, 1]]}, "source": "publisher"},
        ],
    }
    assert facts_from_crossref(message, openalex_is_retracted=False).is_retracted is False


@pytest.mark.parametrize("text,expected", [
    # Duplicate publication: nothing is said about whether the work is right.
    ("An accidental duplication of an already published article.", ReasonClass.PROCEDURAL),
    ("Retracted as a duplicate publication.", ReasonClass.PROCEDURAL),
    # Duplicated images or data inside the paper: the findings are in doubt.
    ("Concerns about image duplication in Figure 3 were substantiated.", ReasonClass.SUBSTANTIVE),
    ("The authors duplicated data across two panels.", ReasonClass.SUBSTANTIVE),
    ("Figures were manipulated.", ReasonClass.SUBSTANTIVE),
])
def test_the_two_senses_of_duplication_are_distinguished(text, expected):
    """Duplicate publication and duplicated figures point opposite ways."""
    assert classify_reason(text)[0] is expected
