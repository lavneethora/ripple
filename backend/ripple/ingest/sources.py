"""Fetching real works and retraction notices from OpenAlex and Crossref.

Both APIs are free and need no key. Raw responses are cached to disk so a
re-run never re-crawls, which keeps the seeder cheap to iterate on and means
the committed store can be rebuilt without hammering anyone's API.

No personal data is sent. OpenAlex offers a faster "polite pool" in exchange
for an email address in the query string; this deliberately does not use it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import requests

OPENALEX = "https://api.openalex.org"
CROSSREF = "https://api.crossref.org"
USER_AGENT = "ripple-hackathon/0.1 (graph research prototype)"

# Only what the schema stores, so payloads stay small.
WORK_FIELDS = (
    "id,doi,display_name,publication_date,publication_year,type,"
    "is_retracted,cited_by_count,referenced_works,authorships,"
    "primary_location,referenced_works_count"
)


class RateLimiter:
    """Minimum spacing between calls. Deliberately polite, not aggressive."""

    def __init__(self, min_interval: float = 0.12) -> None:
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()


@dataclass
class Fetcher:
    """HTTP access with an on-disk cache keyed by request URL."""

    cache_dir: Path
    limiter: RateLimiter = field(default_factory=RateLimiter)
    session: requests.Session = field(default_factory=requests.Session)
    hits: int = 0
    misses: int = 0

    def __post_init__(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session.headers.update({"User-Agent": USER_AGENT})

    def _cache_path(self, key: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in key)[:180]
        return self.cache_dir / f"{safe}.json"

    def get(self, url: str, params: dict[str, Any] | None = None, *, key: str) -> dict[str, Any]:
        path = self._cache_path(key)
        if path.exists():
            self.hits += 1
            return json.loads(path.read_text())

        self.misses += 1
        self.limiter.wait()
        for attempt in range(4):
            try:
                resp = self.session.get(url, params=params, timeout=30)
                if resp.status_code == 429 or resp.status_code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                payload = resp.json()
                path.write_text(json.dumps(payload))
                return payload
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        raise RuntimeError(f"giving up on {url}")


def short_id(openalex_id: str) -> str:
    """'https://openalex.org/W123' -> 'W123'."""
    return openalex_id.rsplit("/", 1)[-1]


def fetch_work(fetcher: Fetcher, work_id: str) -> dict[str, Any]:
    wid = short_id(work_id)
    return fetcher.get(f"{OPENALEX}/works/{wid}", {"select": WORK_FIELDS}, key=f"work_{wid}")


def fetch_works_batch(fetcher: Fetcher, work_ids: list[str]) -> list[dict[str, Any]]:
    """Fetch many works by id. OpenAlex allows up to 50 per OR filter."""
    out: list[dict[str, Any]] = []
    ids = [short_id(w) for w in work_ids]
    for i in range(0, len(ids), 50):
        chunk = ids[i : i + 50]
        payload = fetcher.get(
            f"{OPENALEX}/works",
            {"filter": f"openalex_id:{'|'.join(chunk)}", "per-page": 50, "select": WORK_FIELDS},
            key=f"batch_{chunk[0]}_{len(chunk)}",
        )
        out.extend(payload.get("results", []))
    return out


def iter_citing_works(
    fetcher: Fetcher,
    work_id: str,
    *,
    limit: int,
    work_type: str | None = None,
    sort: str = "cited_by_count:desc",
    from_publication_date: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Works citing `work_id`, most-cited first.

    Sorted rather than sampled so the selection is deterministic and the
    committed store is reproducible.

    `from_publication_date` matters more than it looks. Sorting purely by
    citation count selects the most-cited citing works, and for a 2020 paper
    those are overwhelmingly early-pandemic reviews published before the
    retraction. Without a date floor the seeded set contains almost no
    post-retraction citations, which is the entire phenomenon under study.
    """
    wid = short_id(work_id)
    filt = f"cites:{wid}"
    if work_type:
        filt += f",type:{work_type}"
    if from_publication_date:
        filt += f",from_publication_date:{from_publication_date}"

    seen = 0
    page = 1
    while seen < limit:
        per_page = min(50, limit - seen)
        payload = fetcher.get(
            f"{OPENALEX}/works",
            {"filter": filt, "per-page": per_page, "page": page,
             "sort": sort, "select": WORK_FIELDS},
            key=f"citing_{wid}_{work_type or 'any'}_{sort}"
                f"_{from_publication_date or 'all'}_p{page}_n{per_page}",
        )
        results = payload.get("results", [])
        if not results:
            return
        for work in results:
            yield work
            seen += 1
            if seen >= limit:
                return
        page += 1


def citation_counts_by_year(fetcher: Fetcher, work_id: str) -> dict[int, int]:
    wid = short_id(work_id)
    payload = fetcher.get(
        f"{OPENALEX}/works",
        {"filter": f"cites:{wid}", "group_by": "publication_year"},
        key=f"citing_{wid}_by_year",
    )
    counts: dict[int, int] = {}
    for group in payload.get("group_by", []):
        try:
            counts[int(group["key"])] = int(group["count"])
        except (ValueError, TypeError, KeyError):
            continue
    return counts


def total_citing(fetcher: Fetcher, work_id: str, *, work_type: str | None = None) -> int:
    wid = short_id(work_id)
    filt = f"cites:{wid}" + (f",type:{work_type}" if work_type else "")
    payload = fetcher.get(
        f"{OPENALEX}/works",
        {"filter": filt, "per-page": 1, "select": "id"},
        key=f"count_{wid}_{work_type or 'any'}",
    )
    return int(payload.get("meta", {}).get("count", 0))


def fetch_retraction(fetcher: Fetcher, doi: str) -> dict[str, Any] | None:
    """Retraction notice and date from Crossref.

    OpenAlex carries an is_retracted flag but no retraction date, and the date
    is what makes the bitemporal model work, so Crossref is the source for it.
    """
    clean = doi.replace("https://doi.org/", "").strip()
    try:
        payload = fetcher.get(
            f"{CROSSREF}/works/{clean}", None, key=f"crossref_{clean.replace('/', '_')}"
        )
    except requests.RequestException:
        return None

    message = payload.get("message", {})
    updates = list(message.get("update-to") or []) + list(message.get("updated-by") or [])
    for update in updates:
        if update.get("type") != "retraction":
            continue
        parts = (update.get("updated") or {}).get("date-parts") or [[]]
        if not parts or not parts[0] or len(parts[0]) < 3:
            continue
        year, month, day = parts[0][:3]
        return {
            "retracted_on": f"{year:04d}-{month:02d}-{day:02d}",
            "notice_doi": update.get("DOI"),
            "source": update.get("source", "unknown"),
        }
    return None
