"""RIPPLE as MCP tools, so an agent can ask what its evidence still supports.

This is the same engine the web app uses, behind a second front door. Nothing
here reimplements reasoning: every tool is a thin call into ripple.tms.service
and ripple.graph.queries, so the answers an agent gets and the answers a judge
clicks through in the browser cannot drift apart.

Run it:
    python backend/ripple/mcp_server.py

The server opens a working copy of the committed store, because propagation
writes and Relate rewrites snapshot files on every write.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from ripple.graph.actor import GraphActor
from ripple.graph.queries import Q_SUPPORT_SET
from ripple.ingest.retractions import classify_reason
from ripple.schema.model import LATE_CITATION_DAYS
from ripple.tms.service import (
    post_retraction_breakdown,
    propagate,
    retraction_event,
)

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "data" / "ripple-retractions"
WORK_DIR = ROOT / "data" / ".work"
FOCAL = "W3010930696"

server = MCPServer(
    name="ripple",
    instructions=(
        "RIPPLE answers one question over a graph of scientific works: which "
        "conclusions still stand if a paper was retracted.\n\n"
        "Two things to keep straight when reporting its results.\n\n"
        "A citation is not a justification. The number of works citing a "
        "retracted paper measures exposure, not loss of support, and the two "
        "differ by orders of magnitude. Report them separately.\n\n"
        "Not every retraction means the work is wrong. Procedural retractions "
        "such as duplicate publication say nothing about whether a finding is "
        "correct, and claims depending on them are flagged for review rather "
        "than treated as refuted."
    ),
)

_actor: GraphActor | None = None
_store: Path | None = None


def _get_actor() -> GraphActor:
    global _actor, _store
    if _actor is None:
        if not STORE.exists():
            raise RuntimeError(f"no store at {STORE}; run scripts/seed_demo.py first")
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        _store = Path(tempfile.mkdtemp(prefix="mcp-", dir=WORK_DIR)) / "store"
        shutil.copytree(STORE, _store)
        _actor = GraphActor(_store, readers=1)
    return _actor


@atexit.register
def _cleanup() -> None:
    if _actor is not None:
        _actor.close()
    if _store is not None:
        shutil.rmtree(_store.parent, ignore_errors=True)


@server.tool(
    description=(
        "The retraction under study and how far it reaches. Returns the "
        "retracted work, its date and reason class, and a breakdown of "
        "citations published after the retraction. The citation counts are "
        "exposure, not loss of support."
    )
)
def scenario() -> dict[str, Any]:
    actor = _get_actor()
    event = retraction_event(actor, FOCAL)
    if event is None:
        return {"error": f"no retraction event recorded for {FOCAL}"}
    totals = actor.read("MATCH (n) RETURN count(n) AS n")[0]["n"]
    claims = actor.read("MATCH (c:Claim) RETURN count(c) AS n")[0]["n"]
    return {
        "retracted_work": event,
        "exposure_by_awareness": post_retraction_breakdown(actor, FOCAL),
        "exposure_note": (
            "Counts of citing works. This measures how far the retracted work "
            "reached, and says nothing about which conclusions lose support."
        ),
        "store": {"nodes": totals, "claims": claims},
    }


@server.tool(
    description=(
        "Re-derive every claim in the graph and report what still holds. Set "
        "retracted=false to see what was believed while the work still stood. "
        "Statuses: SUPPORTED, SUPPORTED_WEAKENED, QUESTIONED (an upstream "
        "retraction that does not impeach the findings), INDETERMINATE (rests "
        "on something unassessed), UNSUPPORTED, UNSUPPORTED_CIRCULAR."
    )
)
def what_still_holds(retracted: bool = True, work_id: str = FOCAL) -> dict[str, Any]:
    actor = _get_actor()
    if not retracted:
        actor.write(
            "MATCH (w:Work {openalex_id:$id}) SET w.is_retracted = false, w.label = 'IN'",
            {"id": work_id},
        )
        result = propagate(actor, epoch=1)
    else:
        actor.write(
            "MATCH (w:Work {openalex_id:$id}) SET w.is_retracted = true",
            {"id": work_id},
        )
        result = propagate(actor, retracted_work=work_id, epoch=2)

    return {
        "retraction_applied": retracted,
        "work_id": work_id,
        "summary": result.summary,
        "claims": [
            {
                "claim_id": v.belief_id,
                "status": v.status.value,
                "why": v.reason_code,
                "live_support": v.live_support_count,
                "support_before": v.prior_support_count,
                "derivation": v.witness_chain,
                "failing_justifications": v.killers,
                "unassessed_antecedents": v.missing_antecedents,
            }
            for v in result.verdicts
        ],
    }


@server.tool(
    description=(
        "Why a specific claim holds or fails. Returns every justification "
        "with its antecedents, which of them are live, the threshold needed, "
        "and the provenance tier of the justification itself."
    )
)
def explain_claim(claim_id: str) -> dict[str, Any]:
    actor = _get_actor()
    rows = actor.read(Q_SUPPORT_SET, {"claim_id": claim_id})
    if not rows:
        known = [r["id"] for r in actor.read("MATCH (c:Claim) RETURN c.id AS id ORDER BY id")]
        return {"error": f"no justifications for {claim_id!r}", "known_claims": known}

    current = actor.read(
        "MATCH (c:Claim {id:$id}) RETURN c.text AS text, c.status AS status, "
        "c.reason_code AS reason",
        {"id": claim_id},
    )
    return {
        "claim_id": claim_id,
        "claim": current[0] if current else None,
        "justifications": [
            {
                "id": r["justification_id"],
                "rule": r["rule"],
                "threshold": r["threshold"],
                "live_antecedents": r["n_in"],
                "dead_antecedents": r["n_out"],
                "retracted_antecedents": r["n_retracted"],
                "satisfied": r["n_in"] >= r["threshold"],
                "confidence": r["confidence"],
                "provenance_tier": r["tier"],
                "antecedents": r["antecedents"],
            }
            for r in rows
        ],
    }


@server.tool(
    description=(
        "Works that cite the retracted paper after it was retracted. Only "
        "citations with day-precision dates beyond the in-press grace window "
        "are returned, so the count does not include works that were "
        "plausibly already in press."
    )
)
def post_retraction_citations(limit: int = 15, min_lag_days: int = 1) -> dict[str, Any]:
    actor = _get_actor()
    rows = actor.read(
        "MATCH (citing:Work)-[cit:CITES]->(:Work {openalex_id: $id}) "
        "WHERE cit.post_retraction_lag_days >= $min_lag "
        "  AND cit.awareness = 'unambiguous' "
        "RETURN citing.openalex_id AS work_id, citing.doi AS doi, "
        "       citing.title AS title, citing.type AS type, "
        "       citing.pub_date AS published, "
        "       cit.post_retraction_lag_days AS lag_days "
        "ORDER BY lag_days DESC LIMIT $limit",
        {"id": FOCAL, "min_lag": min_lag_days, "limit": limit},
    )
    return {
        "citations": rows,
        "beyond_18_months": [r for r in rows if r["lag_days"] >= LATE_CITATION_DAYS],
        "note": (
            "Excludes citations within the 180 day in-press grace window and "
            "any work whose publication date is only year-precise."
        ),
    }


@server.tool(
    description=(
        "Classify a retraction notice as SUBSTANTIVE (the findings are in "
        "doubt) or PROCEDURAL (duplicate publication, authorship, and the "
        "like, which say nothing about correctness). Returns UNKNOWN rather "
        "than guessing, and UNKNOWN does not impeach a finding."
    )
)
def classify_retraction_notice(notice_text: str) -> dict[str, Any]:
    reason, excerpt = classify_reason(notice_text)
    return {
        "reason_class": reason.value,
        "matched_on": excerpt,
        "impeaches_findings": reason.value == "SUBSTANTIVE",
        "note": (
            "UNKNOWN deliberately does not impeach. Declaring a conclusion "
            "unsupported on the strength of a reason nobody has read is an "
            "overreach."
        ),
    }


@server.tool(
    description="List every claim in the graph with its current status."
)
def list_claims() -> dict[str, Any]:
    actor = _get_actor()
    return {
        "claims": actor.read(
            "MATCH (c:Claim) RETURN c.id AS id, c.text AS text, "
            "c.status AS status, c.provenance_tier AS provenance_tier "
            "ORDER BY c.id"
        )
    }


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
