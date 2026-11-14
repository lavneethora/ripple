"""Graph schema, the bitemporal time model, and the DDL that enforces them.

Two layers over the same nodes
------------------------------
The schema encodes the project's governing rule structurally rather than by
convention. `REPORTS` says "this work states this claim"; `ANTECEDENT_OF` says
"this claim rests on this belief". They are different relations, and merging
them into one is precisely how a citation silently becomes an entailment.

Only the justification layer feeds the truth maintenance engine. The
bibliographic layer is for measuring exposure and for display.

Time
----
Two independent axes, stored as integer epoch-days:

  valid time      when a fact was true of the world. A Work is valid over
                  [pub_day, retraction_day). A retraction truncates that
                  interval, it never deletes a row.
  knowledge time  when RIPPLE, or the community, knew it.

Dates are integers rather than temporal types for a measured reason. This
build of Relate evaluates `duration.between(date('2020-07-01'),
date('2021-09-13')).days` as 12, because `.days` is the day component of a
14-month-and-12-day breakdown rather than the total. The true gap is 439 days.
A "published 18 months after the retraction" filter written the obvious way
would silently return nonsense. Integer subtraction has no such trap, and
`duration.inDays(...).days` is the correct alternative if a date type is ever
needed. See docs/cypher-compatibility.md.
"""

from __future__ import annotations

import datetime as _dt
from enum import Enum
from typing import Final

_EPOCH: Final = _dt.date(1970, 1, 1)
_EPOCH_ORDINAL: Final = _EPOCH.toordinal()

# A sentinel for "still valid", chosen so that `pub_day < valid_to_day`
# comparisons need no null handling. Far enough out to never be reached.
OPEN_INTERVAL: Final[int] = 10**7


def to_epoch_day(value: _dt.date | str) -> int:
    """Convert a date or ISO 'YYYY-MM-DD' string to days since 1970-01-01."""
    if isinstance(value, str):
        value = _dt.date.fromisoformat(value[:10])
    if isinstance(value, _dt.datetime):
        value = value.date()
    return value.toordinal() - _EPOCH_ORDINAL


def from_epoch_day(day: int) -> _dt.date:
    """Inverse of to_epoch_day."""
    return _dt.date.fromordinal(day + _EPOCH_ORDINAL)


def iso(day: int) -> str:
    """Render an epoch-day as 'YYYY-MM-DD' for display and certificates."""
    return from_epoch_day(day).isoformat()


class Label(str, Enum):
    WORK = "Work"
    CLAIM = "Claim"
    JUSTIFICATION = "Justification"
    RETRACTION_EVENT = "RetractionEvent"
    CLAIM_STATE = "ClaimState"
    AUTHOR = "Author"


class Rel(str, Enum):
    # Bibliographic layer: exposure and display only.
    CITES = "CITES"
    REPORTS = "REPORTS"
    AUTHORED_BY = "AUTHORED_BY"
    # Justification layer: the only edges the engine reasons over.
    ANTECEDENT_OF = "ANTECEDENT_OF"
    SUPPORTS = "SUPPORTS"
    # Bookkeeping.
    HAS_RETRACTION = "HAS_RETRACTION"
    HAS_STATE = "HAS_STATE"


class DatePrecision(str, Enum):
    """How much to trust a publication date.

    OpenAlex frequently carries year-only dates, which materialize as January
    1 and would inflate any "published N days after the retraction" count.
    Only DAY-precision dates are allowed into headline figures.
    """

    DAY = "day"
    MONTH = "month"
    YEAR = "year"


class Awareness(str, Enum):
    """Whether a citing work could reasonably have known about the retraction.

    A paper accepted before a retraction can appear months after it, so
    lateness alone is not culpability. Only UNAMBIGUOUS citations are counted
    in headline numbers.
    """

    UNAMBIGUOUS = "unambiguous"
    PLAUSIBLE_PIPELINE = "plausible_pipeline"
    UNKNOWN_PRECISION = "unknown_precision"


class ReasonClass(str, Enum):
    """Why a work was retracted.

    Roughly a third of retractions are procedural and do not impeach the
    findings, so this drives whether dependents are broken or merely flagged.
    """

    SUBSTANTIVE = "SUBSTANTIVE"
    PROCEDURAL = "PROCEDURAL"
    UNKNOWN = "UNKNOWN"


class ProvenanceTier(str, Enum):
    """Where a justification came from.

    Surfaced next to every status it produces, so hand-curated support is
    never silently presented as equivalent to machine-extracted support.
    """

    CURATED = "curated"
    LLM_EXTRACTED = "llm_extracted"
    HEURISTIC = "heuristic"


# Days after a retraction within which a citation is treated as an in-press
# artifact rather than a knowing one.
GRACE_DAYS: Final[int] = 180
# A reporting bucket for "published more than 18 months after the
# retraction", used to break down counts. It is deliberately NOT the
# threshold for counting a citation as post-retraction: that is GRACE_DAYS.
# Individual documents are always reported with their actual lag, because
# rounding 439 days up to "18 months" would overstate the case.
LATE_CITATION_DAYS: Final[int] = 540


SCHEMA_DDL: Final[tuple[str, ...]] = (
    # Identity. MERGE on these keys is idempotent, which the seeder relies on
    # to be safely re-runnable.
    "CREATE CONSTRAINT work_id_uq IF NOT EXISTS "
    "FOR (w:Work) REQUIRE w.openalex_id IS UNIQUE",
    "CREATE CONSTRAINT claim_id_uq IF NOT EXISTS "
    "FOR (c:Claim) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT just_id_uq IF NOT EXISTS "
    "FOR (j:Justification) REQUIRE j.id IS UNIQUE",
    "CREATE CONSTRAINT retraction_id_uq IF NOT EXISTS "
    "FOR (r:RetractionEvent) REQUIRE r.id IS UNIQUE",
    "CREATE CONSTRAINT author_id_uq IF NOT EXISTS "
    "FOR (a:Author) REQUIRE a.openalex_id IS UNIQUE",
    # Lookups used by the impact and post-retraction queries.
    "CREATE INDEX work_retracted_idx IF NOT EXISTS FOR (w:Work) ON (w.is_retracted)",
    "CREATE INDEX work_pubday_idx IF NOT EXISTS FOR (w:Work) ON (w.pub_day)",
    "CREATE INDEX work_type_idx IF NOT EXISTS FOR (w:Work) ON (w.type)",
    "CREATE INDEX claim_label_idx IF NOT EXISTS FOR (c:Claim) ON (c.label)",
    # Relationship property index: the post-retraction filter reduces to a
    # single comparison on this, so it carries the headline query.
    "CREATE INDEX cites_lag_idx IF NOT EXISTS "
    "FOR ()-[r:CITES]-() ON (r.post_retraction_lag_days)",
    # Bitemporal snapshot reads.
    "CREATE INDEX state_tx_idx IF NOT EXISTS FOR (s:ClaimState) ON (s.tx_from_day)",
    "CREATE INDEX state_valid_idx IF NOT EXISTS FOR (s:ClaimState) ON (s.valid_from_day)",
    # Entry points for finding a claim or work by text.
    "CREATE FULLTEXT INDEX claim_text_ft IF NOT EXISTS FOR (c:Claim) ON EACH [c.text]",
    "CREATE FULLTEXT INDEX work_title_ft IF NOT EXISTS FOR (w:Work) ON EACH [w.title]",
)


def apply_schema(actor) -> int:
    """Create constraints and indexes. Idempotent, so safe to run on boot."""
    for statement in SCHEMA_DDL:
        actor.write(statement)
    return len(SCHEMA_DDL)


def classify_awareness(
    citing_pub_day: int,
    retraction_day: int,
    precision: DatePrecision,
    *,
    grace_days: int = GRACE_DAYS,
) -> Awareness:
    """Decide whether a post-retraction citation was plausibly knowing.

    Deliberately conservative: anything short of a day-precision date beyond
    the grace window is excluded from headline counts.
    """
    if precision is not DatePrecision.DAY:
        return Awareness.UNKNOWN_PRECISION
    if citing_pub_day - retraction_day <= grace_days:
        return Awareness.PLAUSIBLE_PIPELINE
    return Awareness.UNAMBIGUOUS
