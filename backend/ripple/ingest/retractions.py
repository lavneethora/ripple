"""Deciding whether a work is really retracted, and whether that impeaches it.

Two problems this exists to solve, both found in the seeded data.

OpenAlex `is_retracted` produces false positives. The 2020 case report
10.1016/j.jaccas.2020.05.023 is flagged retracted there and has no retraction
or update record in Crossref at all. What was actually withdrawn is a
duplicate of it published at a different DOI, and the flag appears to have
been attached to the surviving original. Crossref is therefore treated as
authoritative for whether a retraction exists.

Not every retraction impeaches the findings. That same duplicate was
withdrawn because it was "an accidental duplication of an article that has
already been published", which says nothing about whether the work is
correct. Marking everything downstream of it as broken would be wrong about
the world, so reasons are classified and procedural ones only flag their
dependents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ripple.schema.model import ReasonClass

#: "Duplication" is ambiguous and the two senses point opposite ways.
#: Duplicate PUBLICATION is procedural; duplicated IMAGES or DATA inside a
#: paper is figure manipulation and is substantive. These specific senses are
#: matched before the generic procedural patterns below, which would
#: otherwise swallow them.
SUBSTANTIVE_OVERRIDES: tuple[str, ...] = (
    r"\b(image|figure|panel|blot|data)s?\s+(duplicat|manipulat)",
    r"\bduplicat\w*\s+(image|figure|panel|blot|data)",
)

#: Reasons that do not call the findings into question. The work may be
#: perfectly correct; something about its publication was not.
PROCEDURAL_PATTERNS: tuple[str, ...] = (
    r"\bduplicat",            # accidental duplication, duplicate publication
    r"\bplagiar",
    r"\bauthorship\b",
    r"\bcopyright\b",
    r"\bpermission\b",
    r"\badministrative (error|reasons?)\b",
    r"\baccidental",
    r"\bpublisher(?:'s)? error\b",
    r"\bwithdrawn by the publisher\b",
)

#: Reasons that do call the findings into question.
SUBSTANTIVE_PATTERNS: tuple[str, ...] = (
    r"\bfabricat",
    r"\bfalsif",
    r"\bunreliable\b",
    r"\birreproducib",
    r"\bdata (integrity|manipulation|error)",
    r"\bmanipulat",
    r"\bflawed\b",
    r"\binvalid\b",
    r"\bmisconduct\b",
    r"\bimage (duplication|manipulation)\b",
    r"\bethic(s|al) (approval|concerns?)\b",
    r"\bconsent\b",
)


@dataclass
class RetractionFacts:
    """What is actually known about a work's retraction status."""

    is_retracted: bool
    retracted_on: str | None = None
    notice_doi: str | None = None
    source: str | None = None
    reason_class: ReasonClass = ReasonClass.UNKNOWN
    reason_text: str | None = None
    openalex_disagrees: bool = False

    @property
    def impeaches_findings(self) -> bool:
        """Whether dependents should lose support rather than be flagged.

        UNKNOWN is deliberately not treated as impeaching. Asserting that a
        conclusion is unsupported on the strength of a reason nobody has read
        is the kind of overreach this project is meant to avoid.
        """
        return self.is_retracted and self.reason_class is ReasonClass.SUBSTANTIVE


def classify_reason(text: str | None) -> tuple[ReasonClass, str | None]:
    """Classify a retraction notice from its text.

    Order matters. The substantive overrides go first so that "image
    duplication" is not read as duplicate publication. Procedural patterns go
    next, so a notice saying a paper is a duplicate stays procedural even if
    it also contains a word like "error". Anything unmatched stays UNKNOWN
    rather than being guessed.
    """
    if not text:
        return ReasonClass.UNKNOWN, None
    lowered = text.lower()
    for pattern in SUBSTANTIVE_OVERRIDES:
        m = re.search(pattern, lowered)
        if m:
            return ReasonClass.SUBSTANTIVE, _excerpt(text, m.start())
    for pattern in PROCEDURAL_PATTERNS:
        m = re.search(pattern, lowered)
        if m:
            return ReasonClass.PROCEDURAL, _excerpt(text, m.start())
    for pattern in SUBSTANTIVE_PATTERNS:
        m = re.search(pattern, lowered)
        if m:
            return ReasonClass.SUBSTANTIVE, _excerpt(text, m.start())
    return ReasonClass.UNKNOWN, None


def _excerpt(text: str, at: int, width: int = 90) -> str:
    start = max(0, at - width // 3)
    return text[start : start + width].strip()


def facts_from_crossref(
    message: dict[str, Any] | None,
    *,
    openalex_is_retracted: bool,
    notice_text: str | None = None,
) -> RetractionFacts:
    """Build retraction facts, trusting Crossref over OpenAlex.

    A work OpenAlex flags with no corresponding Crossref record is reported
    as not retracted, with the disagreement recorded rather than hidden.
    """
    if not message:
        return RetractionFacts(
            is_retracted=False, openalex_disagrees=openalex_is_retracted
        )

    records = [
        u
        for key in ("update-to", "updated-by")
        for u in (message.get(key) or [])
        if u.get("type") in {"retraction", "withdrawal"}
    ]
    if not records:
        return RetractionFacts(
            is_retracted=False, openalex_disagrees=openalex_is_retracted
        )

    def when(update: dict[str, Any]) -> tuple[int, int, int]:
        parts = (update.get("updated") or {}).get("date-parts") or [[]]
        p = (parts[0] + [1, 1, 1])[:3] if parts and parts[0] else [9999, 1, 1]
        return tuple(p)  # type: ignore[return-value]

    earliest = min(records, key=when)
    year, month, day = when(earliest)
    if year == 9999:
        return RetractionFacts(
            is_retracted=False, openalex_disagrees=openalex_is_retracted
        )

    # The notice text, where we have it, plus the title: a title beginning
    # "WITHDRAWN:" or "RETRACTED:" is itself weak evidence of the kind.
    title = (message.get("title") or [""])[0]
    blob = " ".join(filter(None, (notice_text, title)))
    reason_class, excerpt = classify_reason(blob)

    return RetractionFacts(
        is_retracted=True,
        retracted_on=f"{year:04d}-{month:02d}-{day:02d}",
        notice_doi=earliest.get("DOI"),
        source=earliest.get("source"),
        reason_class=reason_class,
        reason_text=excerpt,
        openalex_disagrees=not openalex_is_retracted,
    )


# --- curated overrides -----------------------------------------------------


@dataclass
class CuratedReason:
    """A retraction reason read from the notice by a human, with its source."""

    work_id: str
    reason_class: ReasonClass
    summary: str
    source: str
    source_type: str
    impeaches_findings: bool
    retracted_on: str | None = None


def load_curated_reasons(path: Path) -> tuple[dict[str, CuratedReason], dict[str, str]]:
    """Load curated reasons and disputed OpenAlex flags.

    Crossref records that a retraction happened, rarely why. Where the reason
    is documented only in prose, it is recorded by hand with a citable source
    rather than guessed, because an unread reason must not be allowed to
    decide whether downstream conclusions hold.
    """
    if not path.exists():
        return {}, {}
    data = yaml.safe_load(path.read_text()) or {}

    reasons: dict[str, CuratedReason] = {}
    for entry in data.get("reasons") or []:
        for key in ("work_id", "reason_class", "summary", "source", "impeaches_findings"):
            if key not in entry:
                raise ValueError(f"curated reason missing {key!r}: {entry.get('work_id')}")
        reason_class = ReasonClass(entry["reason_class"])
        if reason_class is ReasonClass.SUBSTANTIVE and not entry["impeaches_findings"]:
            raise ValueError(
                f"{entry['work_id']}: SUBSTANTIVE must impeach findings"
            )
        if reason_class is ReasonClass.PROCEDURAL and entry["impeaches_findings"]:
            raise ValueError(
                f"{entry['work_id']}: PROCEDURAL must not impeach findings"
            )
        reasons[entry["work_id"]] = CuratedReason(
            work_id=entry["work_id"],
            reason_class=reason_class,
            summary=" ".join(entry["summary"].split()),
            source=entry["source"],
            source_type=entry.get("source_type", "unknown"),
            impeaches_findings=bool(entry["impeaches_findings"]),
            retracted_on=entry.get("retracted_on"),
        )

    disputed = {
        d["work_id"]: " ".join((d.get("note") or "").split())
        for d in (data.get("disputed_flags") or [])
    }
    return reasons, disputed


def apply_curated(facts: RetractionFacts, curated: CuratedReason | None) -> RetractionFacts:
    """Overlay a curated reason onto machine-derived facts.

    The curated entry supplies only the reason. Whether a retraction exists,
    and its date, still come from Crossref, so a curated note cannot invent a
    retraction that the record does not show.
    """
    if curated is None or not facts.is_retracted:
        return facts
    return RetractionFacts(
        is_retracted=facts.is_retracted,
        retracted_on=facts.retracted_on,
        notice_doi=facts.notice_doi,
        source=facts.source,
        reason_class=curated.reason_class,
        reason_text=curated.summary,
        openalex_disagrees=facts.openalex_disagrees,
    )
