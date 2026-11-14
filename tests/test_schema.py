"""Schema, time model, and DDL."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import relate

from ripple.graph.actor import GraphActor
from ripple.schema.model import (
    GRACE_DAYS,
    LATE_CITATION_DAYS,
    SCHEMA_DDL,
    Awareness,
    DatePrecision,
    apply_schema,
    classify_awareness,
    from_epoch_day,
    iso,
    to_epoch_day,
)

RETRACTION = "2020-07-01"   # Gautret et al, hydroxychloroquine
CITING = "2021-09-13"       # ESC COVID-19 guidance
TRUE_LAG_DAYS = 439


@pytest.fixture()
def actor(tmp_path: Path):
    a = GraphActor(tmp_path / "store", readers=1)
    yield a
    a.close()


# --- time model ---

def test_epoch_day_matches_unix_epoch():
    assert to_epoch_day("1970-01-01") == 0
    assert to_epoch_day(RETRACTION) == 18444


def test_epoch_day_roundtrips():
    for iso_date in ("1970-01-01", "2020-02-29", RETRACTION, CITING, "2026-12-11"):
        assert iso(to_epoch_day(iso_date)) == iso_date


def test_accepts_date_and_datetime_and_iso_string():
    d = dt.date(2020, 7, 1)
    assert to_epoch_day(d) == to_epoch_day(RETRACTION)
    assert to_epoch_day(dt.datetime(2020, 7, 1, 23, 59)) == to_epoch_day(RETRACTION)
    assert from_epoch_day(to_epoch_day(d)) == d


def test_integer_lag_is_correct_where_duration_between_is_not():
    """Guards the reason dates are integers rather than temporal types.

    This build evaluates duration.between(a, b).days as the day component of
    a Y/M/D breakdown, so it returns 12 over this interval where the answer
    is 439. Integer subtraction has no such trap.
    """
    assert to_epoch_day(CITING) - to_epoch_day(RETRACTION) == TRUE_LAG_DAYS

    conn = relate.connect(":memory:")
    try:
        wrong = dict(
            conn.execute(
                "RETURN duration.between(date($a), date($b)).days AS d",
                {"a": RETRACTION, "b": CITING},
            ).fetchall()[0]
        )["d"]
        right = dict(
            conn.execute(
                "RETURN duration.inDays(date($a), date($b)).days AS d",
                {"a": RETRACTION, "b": CITING},
            ).fetchall()[0]
        )["d"]
    finally:
        conn.close()

    assert wrong == 12, "the trap this model exists to avoid has changed shape"
    assert right == TRUE_LAG_DAYS
    assert wrong != TRUE_LAG_DAYS


def test_the_esc_guidance_is_a_late_citation():
    """The flagship demo fact, pinned so the claim cannot drift.

    The ESC guidance was published 439 days after the retraction, which is
    about 14 months. That is well past the in-press grace window, so it counts
    as an unambiguous post-retraction citation, but it does NOT clear the
    18-month reporting bucket. Stating 18 months for this document would
    overstate it, so the exact figure is asserted here.
    """
    lag = to_epoch_day(CITING) - to_epoch_day(RETRACTION)
    assert lag == TRUE_LAG_DAYS
    assert lag > GRACE_DAYS, "past the in-press grace window"
    assert 14 <= lag / 30.44 < 15, "about 14 months, not 18"
    assert lag < LATE_CITATION_DAYS, "does not clear the 18-month bucket"
    assert (
        classify_awareness(to_epoch_day(CITING), to_epoch_day(RETRACTION), DatePrecision.DAY)
        is Awareness.UNAMBIGUOUS
    )


# --- awareness ---

@pytest.mark.parametrize(
    "lag,precision,expected",
    [
        (439, DatePrecision.DAY, Awareness.UNAMBIGUOUS),
        (181, DatePrecision.DAY, Awareness.UNAMBIGUOUS),
        (180, DatePrecision.DAY, Awareness.PLAUSIBLE_PIPELINE),
        (1, DatePrecision.DAY, Awareness.PLAUSIBLE_PIPELINE),
        (439, DatePrecision.YEAR, Awareness.UNKNOWN_PRECISION),
        (439, DatePrecision.MONTH, Awareness.UNKNOWN_PRECISION),
    ],
)
def test_awareness_classification(lag, precision, expected):
    r = to_epoch_day(RETRACTION)
    assert classify_awareness(r + lag, r, precision) is expected


def test_year_only_dates_never_count_as_unambiguous():
    """OpenAlex year-only dates land on Jan 1 and would inflate the headline."""
    r = to_epoch_day(RETRACTION)
    for lag in (200, 400, 1000, 2000):
        assert classify_awareness(r + lag, r, DatePrecision.YEAR) is Awareness.UNKNOWN_PRECISION


# --- DDL ---

def test_schema_applies(actor: GraphActor):
    assert apply_schema(actor) == len(SCHEMA_DDL)
    names = {r["name"] for r in actor.write("SHOW INDEXES")}
    assert "work_pubday_idx" in names
    assert "cites_lag_idx" in names, "the post-retraction filter depends on this"
    assert "claim_text_ft" in names


def test_schema_is_idempotent(actor: GraphActor):
    apply_schema(actor)
    before = len(actor.write("SHOW INDEXES"))
    apply_schema(actor)
    assert len(actor.write("SHOW INDEXES")) == before


def test_work_identity_is_enforced(actor: GraphActor):
    apply_schema(actor)
    actor.write("CREATE (:Work {openalex_id: 'W1'})")
    with pytest.raises(relate.IntegrityError):
        actor.write("CREATE (:Work {openalex_id: 'W1'})")


def test_merge_is_safely_rerunnable(actor: GraphActor):
    """The seeder re-runs, so MERGE must not duplicate works."""
    apply_schema(actor)
    for _ in range(3):
        actor.write(
            "UNWIND $rows AS r MERGE (w:Work {openalex_id: r.id}) SET w.title = r.t",
            {"rows": [{"id": "W1", "t": "a"}, {"id": "W2", "t": "b"}]},
        )
    assert actor.write("MATCH (w:Work) RETURN count(w) AS n")[0]["n"] == 2


def test_fulltext_index_is_queryable(actor: GraphActor):
    apply_schema(actor)
    actor.write(
        "CREATE (:Claim {id: 'c1', text: 'hydroxychloroquine reduces viral carriage duration'})"
    )
    rows = actor.write(
        "CALL db.index.fulltext.queryNodes('claim_text_ft', $q) YIELD node, score "
        "RETURN node.id AS id ORDER BY score DESC",
        {"q": "hydroxychloroquine"},
    )
    assert [r["id"] for r in rows] == ["c1"]
