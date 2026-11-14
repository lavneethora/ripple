"""Build the persistent Relate store from real OpenAlex and Crossref data.

Selection is deterministic (sorted, never sampled) so the committed store is
reproducible and the golden-certificate test is stable.

This writes the bibliographic layer only: works, citations, authors and the
retraction event. The justification layer is curated separately, because
OpenAlex knows who cited whom but not what rests on what, and inferring
entailment from citation is the one mistake this project exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ripple.graph.actor import GraphActor
from ripple.ingest.sources import (
    Fetcher,
    fetch_retraction,
    fetch_works_batch,
    iter_citing_works,
    short_id,
    total_citing,
)
from ripple.schema.model import (
    OPEN_INTERVAL,
    Awareness,
    DatePrecision,
    ReasonClass,
    apply_schema,
    classify_awareness,
    iso,
    to_epoch_day,
)

# Guards an in-memory engine against an unbounded citation closure.
MAX_WORKS = 2_000


@dataclass
class SeedPlan:
    """What to pull. Defaults describe the flagship demo."""

    focal_work: str = "W3010930696"       # Gautret 2020, hydroxychloroquine
    deep_dive: str = "W3216235612"        # ESC COVID-19 guidance, 2021-09-13
    n_citing_reviews: int = 30
    # Only works published after the retraction. Without this the most-cited
    # citers are all pre-retraction and the store shows none of the effect.
    citers_after_retraction_only: bool = True
    include_deep_dive_references: bool = True
    max_works: int = MAX_WORKS


@dataclass
class SeedReport:
    works: int = 0
    citations: int = 0
    authors: int = 0
    retractions: int = 0
    post_retraction_citations: int = 0
    unambiguous_post_retraction: int = 0
    skipped_over_budget: int = 0
    cache_hits: int = 0
    cache_misses: int = 0


def _precision(publication_date: str | None) -> DatePrecision:
    """Infer how much to trust a publication date.

    OpenAlex renders year-only records as January 1, which is
    indistinguishable from a genuine New Year's Day publication. Treating both
    as year-precision loses a few real dates and keeps the post-retraction
    count honest, which is the right side to err on.
    """
    if not publication_date:
        return DatePrecision.YEAR
    if publication_date.endswith("-01-01"):
        return DatePrecision.YEAR
    return DatePrecision.DAY


def _work_row(work: dict[str, Any]) -> dict[str, Any]:
    pub_date = work.get("publication_date")
    location = work.get("primary_location") or {}
    source = (location.get("source") or {}) if isinstance(location, dict) else {}
    return {
        "openalex_id": short_id(work["id"]),
        "doi": (work.get("doi") or "").replace("https://doi.org/", "") or None,
        "title": work.get("display_name") or "(untitled)",
        "type": work.get("type") or "unknown",
        "venue": (source.get("display_name") if isinstance(source, dict) else None),
        "pub_date": pub_date,
        "pub_day": to_epoch_day(pub_date) if pub_date else None,
        "date_precision": _precision(pub_date).value,
        "is_retracted": bool(work.get("is_retracted")),
        "cited_by_count": work.get("cited_by_count") or 0,
        "label": "IN",
    }


UPSERT_WORKS = """
UNWIND $rows AS row
MERGE (w:Work {openalex_id: row.openalex_id})
SET w.doi = row.doi,
    w.title = row.title,
    w.type = row.type,
    w.venue = row.venue,
    w.pub_date = row.pub_date,
    w.pub_day = row.pub_day,
    w.date_precision = row.date_precision,
    w.is_retracted = row.is_retracted,
    w.cited_by_count = row.cited_by_count,
    w.label = coalesce(w.label, row.label)
RETURN count(w) AS n
"""

UPSERT_CITATIONS = """
UNWIND $rows AS row
MATCH (citing:Work {openalex_id: row.citing})
MATCH (cited:Work {openalex_id: row.cited})
MERGE (citing)-[r:CITES]->(cited)
SET r.citing_pub_day = row.citing_pub_day,
    r.post_retraction_lag_days = row.lag,
    r.awareness = row.awareness
RETURN count(r) AS n
"""

UPSERT_AUTHORS = """
UNWIND $rows AS row
MERGE (a:Author {openalex_id: row.author_id})
SET a.display_name = row.display_name
WITH a, row
MATCH (w:Work {openalex_id: row.work_id})
MERGE (w)-[r:AUTHORED_BY]->(a)
SET r.position = row.position
RETURN count(r) AS n
"""

UPSERT_RETRACTION = """
MATCH (w:Work {openalex_id: $work_id})
MERGE (w)-[:HAS_RETRACTION]->(re:RetractionEvent {id: $event_id})
SET re.valid_to_day = $valid_to_day,
    re.announced_day = $announced_day,
    re.tx_from_day = $tx_from_day,
    re.retracted_on = $retracted_on,
    re.notice_doi = $notice_doi,
    re.reason_class = $reason_class,
    re.source = $source
SET w.is_retracted = true,
    w.retraction_day = $valid_to_day
RETURN re.id AS id
"""


def _author_rows(work: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for position, authorship in enumerate(work.get("authorships") or []):
        author = authorship.get("author") or {}
        aid = author.get("id")
        if not aid:
            continue
        rows.append(
            {
                "author_id": short_id(aid),
                "display_name": author.get("display_name") or "(unknown)",
                "work_id": short_id(work["id"]),
                "position": position,
            }
        )
    return rows


def seed(
    actor: GraphActor,
    fetcher: Fetcher,
    plan: SeedPlan | None = None,
    *,
    verbose: bool = True,
) -> SeedReport:
    plan = plan or SeedPlan()
    report = SeedReport()
    say = print if verbose else (lambda *a, **k: None)

    apply_schema(actor)

    # --- focal retracted work and its retraction date ---
    say(f"Fetching focal work {plan.focal_work}")
    focal = fetch_works_batch(fetcher, [plan.focal_work])[0]
    collected: dict[str, dict[str, Any]] = {short_id(focal["id"]): focal}

    retraction = fetch_retraction(fetcher, focal.get("doi") or "")
    if retraction is None:
        raise RuntimeError(
            f"no Crossref retraction date for {plan.focal_work}; "
            "the bitemporal model needs one"
        )
    retraction_day = to_epoch_day(retraction["retracted_on"])
    say(f"  retracted {retraction['retracted_on']} (epoch day {retraction_day})")

    # --- citing reviews, most cited first ---
    floor = None
    if plan.citers_after_retraction_only:
        floor = iso(retraction_day + 1)
    say(f"Fetching top {plan.n_citing_reviews} citing reviews"
        + (f" published from {floor}" if floor else ""))
    citing: list[dict[str, Any]] = list(
        iter_citing_works(
            fetcher,
            plan.focal_work,
            limit=plan.n_citing_reviews,
            work_type="review",
            from_publication_date=floor,
        )
    )
    for work in citing:
        collected[short_id(work["id"])] = work

    # --- the deep-dive document's own reference list ---
    # Needed so independent corroboration is real: without the other
    # references there is nothing for a surviving conclusion to rest on.
    deep_dive_refs: list[str] = []
    if plan.include_deep_dive_references:
        say(f"Fetching deep-dive document {plan.deep_dive}")
        deep = fetch_works_batch(fetcher, [plan.deep_dive])[0]
        collected[short_id(deep["id"])] = deep
        deep_dive_refs = [short_id(r) for r in (deep.get("referenced_works") or [])]
        say(f"  {len(deep_dive_refs)} references")

        budget = plan.max_works - len(collected)
        wanted = deep_dive_refs[:budget]
        report.skipped_over_budget += max(0, len(deep_dive_refs) - len(wanted))
        for work in fetch_works_batch(fetcher, wanted):
            collected.setdefault(short_id(work["id"]), work)

    if len(collected) > plan.max_works:
        raise RuntimeError(f"{len(collected)} works exceeds budget {plan.max_works}")

    # --- write works ---
    rows = [_work_row(w) for w in collected.values()]
    rows.sort(key=lambda r: r["openalex_id"])  # determinism
    actor.write(UPSERT_WORKS, {"rows": rows})
    report.works = len(rows)
    say(f"Wrote {report.works} works")

    # --- retraction event ---
    actor.write(
        UPSERT_RETRACTION,
        {
            "work_id": short_id(focal["id"]),
            "event_id": f"RE_{short_id(focal['id'])}",
            "valid_to_day": retraction_day,
            "announced_day": retraction_day,
            "tx_from_day": retraction_day,
            "retracted_on": retraction["retracted_on"],
            "notice_doi": retraction.get("notice_doi"),
            "reason_class": ReasonClass.SUBSTANTIVE.value,
            "source": retraction.get("source", "unknown"),
        },
    )
    report.retractions = 1

    # --- citation edges ---
    known = set(collected)
    citation_rows: list[dict[str, Any]] = []

    def add_citation(citing_id: str, cited_id: str, citing_work: dict[str, Any]) -> None:
        if citing_id not in known or cited_id not in known:
            return
        pub_date = citing_work.get("publication_date")
        pub_day = to_epoch_day(pub_date) if pub_date else None
        lag = None
        awareness = Awareness.UNKNOWN_PRECISION.value
        if cited_id == short_id(focal["id"]) and pub_day is not None:
            lag = pub_day - retraction_day
            awareness = classify_awareness(
                pub_day, retraction_day, _precision(pub_date)
            ).value
        citation_rows.append(
            {
                "citing": citing_id,
                "cited": cited_id,
                "citing_pub_day": pub_day,
                "lag": lag,
                "awareness": awareness,
            }
        )

    focal_id = short_id(focal["id"])
    for work in citing:
        add_citation(short_id(work["id"]), focal_id, work)
    if plan.include_deep_dive_references:
        deep = collected[short_id(plan.deep_dive)]
        add_citation(short_id(deep["id"]), focal_id, deep)
        for ref in deep_dive_refs:
            add_citation(short_id(deep["id"]), ref, deep)

    # Deduplicate: the deep-dive document may also appear among citing reviews.
    seen_pairs: set[tuple[str, str]] = set()
    unique_rows = []
    for row in sorted(citation_rows, key=lambda r: (r["citing"], r["cited"])):
        pair = (row["citing"], row["cited"])
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        unique_rows.append(row)

    actor.write(UPSERT_CITATIONS, {"rows": unique_rows})
    report.citations = len(unique_rows)
    report.post_retraction_citations = sum(
        1 for r in unique_rows if r["lag"] is not None and r["lag"] > 0
    )
    report.unambiguous_post_retraction = sum(
        1 for r in unique_rows if r["awareness"] == Awareness.UNAMBIGUOUS.value
    )
    say(f"Wrote {report.citations} citation edges")

    # --- authors, for the independence check on surviving support ---
    author_rows: list[dict[str, Any]] = []
    for work in collected.values():
        author_rows.extend(_author_rows(work))
    author_rows.sort(key=lambda r: (r["work_id"], r["position"]))
    if author_rows:
        actor.write(UPSERT_AUTHORS, {"rows": author_rows})
    report.authors = len({r["author_id"] for r in author_rows})
    say(f"Wrote {report.authors} authors")

    report.cache_hits = fetcher.hits
    report.cache_misses = fetcher.misses
    return report
