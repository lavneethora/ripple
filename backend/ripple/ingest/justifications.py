"""Load the justification layer from YAML into the graph.

This layer cannot be derived from OpenAlex. Citation data records who cited
whom; it does not record what rests on what. Inferring one from the other is
the mistake the whole project is built to avoid, so justifications are
authored by hand and every one carries a provenance tier that travels with
any status it produces.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ripple.graph.queries import (
    Q_SET_WORK_LABELS,
    Q_UPSERT_ANTECEDENTS_CLAIM,
    Q_UPSERT_ANTECEDENTS_WORK,
    Q_UPSERT_CLAIMS,
    Q_UPSERT_JUSTIFICATIONS,
    Q_UPSERT_REPORTS,
)
from ripple.schema.model import ProvenanceTier, to_epoch_day


class JustificationError(ValueError):
    """The authored justification file is not usable as written."""


@dataclass
class LoadReport:
    claims: int = 0
    justifications: int = 0
    antecedent_edges: int = 0
    reports_edges: int = 0
    tiers: dict[str, int] | None = None


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise JustificationError(f"{where}: missing required key {key!r}")
    return mapping[key]


def load_file(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise JustificationError(f"{path}: top level must be a mapping")
    return data


def validate(data: dict[str, Any]) -> None:
    """Fail loudly on anything that would quietly corrupt a verdict."""
    claims = data.get("claims") or []
    justifications = data.get("justifications") or []

    claim_ids: set[str] = set()
    for claim in claims:
        cid = _require(claim, "id", "claim")
        if cid in claim_ids:
            raise JustificationError(f"duplicate claim id {cid!r}")
        claim_ids.add(cid)
        _require(claim, "text", f"claim {cid}")
        tier = claim.get("provenance_tier", ProvenanceTier.CURATED.value)
        if tier not in {t.value for t in ProvenanceTier}:
            raise JustificationError(f"claim {cid}: unknown provenance_tier {tier!r}")
        label = claim.get("label", "IN")
        if label not in {"IN", "OUT", "UNKNOWN"}:
            raise JustificationError(f"claim {cid}: unknown label {label!r}")

    seen_j: set[str] = set()
    for just in justifications:
        jid = _require(just, "id", "justification")
        if jid in seen_j:
            raise JustificationError(f"duplicate justification id {jid!r}")
        seen_j.add(jid)

        consequent = _require(just, "consequent", f"justification {jid}")
        if consequent not in claim_ids:
            raise JustificationError(
                f"justification {jid}: consequent {consequent!r} is not a declared claim"
            )

        antecedents = _require(just, "antecedents", f"justification {jid}")
        if not antecedents:
            raise JustificationError(f"justification {jid}: no antecedents")
        if len(set(antecedents)) != len(antecedents):
            # A repeated antecedent would let one belief satisfy a threshold
            # twice and silently inflate support.
            raise JustificationError(f"justification {jid}: duplicate antecedents")

        threshold = just.get("threshold")
        if threshold is not None and not 1 <= threshold <= len(antecedents):
            raise JustificationError(
                f"justification {jid}: threshold {threshold} outside 1..{len(antecedents)}"
            )

        tier = just.get("provenance_tier", ProvenanceTier.CURATED.value)
        if tier not in {t.value for t in ProvenanceTier}:
            raise JustificationError(f"justification {jid}: unknown provenance_tier {tier!r}")


def load(actor, path: Path, *, created_on: str = "2026-11-13") -> LoadReport:
    data = load_file(path)
    validate(data)

    claims = data.get("claims") or []
    justifications = data.get("justifications") or []
    report = LoadReport(tiers={})

    claim_rows = [
        {
            "id": c["id"],
            "text": c["text"],
            "kind": c.get("kind", "finding"),
            "provenance_tier": c.get("provenance_tier", ProvenanceTier.CURATED.value),
            # UNKNOWN marks a belief the system has not adjudicated, which is
            # what separates "we cannot tell" from "it does not hold".
            "label": c.get("label", "IN"),
            # A readable name for the canvas. Falls back to the id.
            "short": c.get("short") or c["id"],
        }
        for c in claims
    ]
    if claim_rows:
        actor.write(Q_UPSERT_CLAIMS, {"rows": sorted(claim_rows, key=lambda r: r["id"])})
    report.claims = len(claim_rows)

    reports_rows = [
        {"work_id": work_id, "claim_id": c["id"], "section": c.get("section")}
        for c in claims
        for work_id in (c.get("reported_by") or [])
    ]
    if reports_rows:
        actor.write(Q_UPSERT_REPORTS, {"rows": reports_rows})
    report.reports_edges = len(reports_rows)

    created_day = to_epoch_day(created_on)
    just_rows = []
    for j in justifications:
        antecedents = list(j["antecedents"])
        tier = j.get("provenance_tier", ProvenanceTier.CURATED.value)
        report.tiers[tier] = report.tiers.get(tier, 0) + 1
        just_rows.append(
            {
                "id": j["id"],
                "consequent": j["consequent"],
                "rule": j.get("rule", "unspecified"),
                "threshold": j.get("threshold") or len(antecedents),
                "arity": len(antecedents),
                "confidence": float(j.get("confidence", 1.0)),
                "provenance_tier": tier,
                "created_day": created_day,
            }
        )
    if just_rows:
        actor.write(Q_UPSERT_JUSTIFICATIONS, {"rows": sorted(just_rows, key=lambda r: r["id"])})
    report.justifications = len(just_rows)

    # Antecedents can be Works or Claims. Route by whether the id names a
    # declared claim; anything else is treated as an OpenAlex work id.
    claim_ids = {c["id"] for c in claims}
    work_edges, claim_edges = [], []
    for j in justifications:
        for antecedent in j["antecedents"]:
            row = {
                "justification_id": j["id"],
                "antecedent": antecedent,
                "role": j.get("role", "support"),
            }
            (claim_edges if antecedent in claim_ids else work_edges).append(row)

    if work_edges:
        written = actor.write(Q_UPSERT_ANTECEDENTS_WORK, {"rows": work_edges})[0]["n"]
        if written != len(work_edges):
            # A silently dropped antecedent weakens a threshold without
            # anyone noticing, which would corrupt every downstream verdict.
            raise JustificationError(
                f"{len(work_edges) - written} antecedent work(s) not found in the store; "
                "seed the bibliographic layer first"
            )
    if claim_edges:
        actor.write(Q_UPSERT_ANTECEDENTS_CLAIM, {"rows": claim_edges})
    report.antecedent_edges = len(work_edges) + len(claim_edges)

    label_rows = [
        {"work_id": wid, "short": short}
        for wid, short in sorted((data.get("work_labels") or {}).items())
    ]
    if label_rows:
        actor.write(Q_SET_WORK_LABELS, {"rows": label_rows})

    return report
