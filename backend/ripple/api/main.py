"""HTTP API over the truth maintenance engine.

The server opens a working copy of the committed store rather than the store
itself. Propagation writes, and Relate rewrites content-addressed snapshot
files on every write, so serving from the committed artifact would leave it
dirty the moment anyone loaded the page.

Every Relate call goes through GraphActor, which pins each connection to its
own thread. FastAPI hands requests to arbitrary workers, so touching a
connection directly from a handler would raise WrongThread intermittently.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ripple.graph.actor import GraphActor
from ripple.graph.queries import Q_SUPPORT_SET, q_blast_radius
from ripple.tms.engine import Status
from ripple.tms.service import (
    HOLDS,
    post_retraction_breakdown,
    propagate,
    retraction_event,
)

ROOT = Path(__file__).resolve().parents[3]
STORE = ROOT / "data" / "ripple-retractions"
WORK_DIR = ROOT / "data" / ".work"
FOCAL = "W3010930696"

#: Claim ids are opaque to the UI, so the API ships display metadata with them.
STATUS_META: dict[str, dict[str, str]] = {
    Status.SUPPORTED.value: {"label": "Holds", "tone": "ok"},
    Status.SUPPORTED_WEAKENED.value: {"label": "Holds, weakened", "tone": "warn"},
    Status.UNSUPPORTED.value: {"label": "Loses support", "tone": "bad"},
    Status.UNSUPPORTED_CIRCULAR.value: {"label": "Circular only", "tone": "bad"},
    Status.INDETERMINATE.value: {"label": "Cannot tell", "tone": "unknown"},
    Status.QUESTIONED.value: {"label": "Questioned", "tone": "warn"},
    Status.RETRACTED.value: {"label": "Retracted", "tone": "bad"},
    Status.UNAFFECTED.value: {"label": "Unaffected", "tone": "ok"},
}


class AppState:
    actor: GraphActor | None = None
    store: Path | None = None


state = AppState()


def _working_copy() -> Path:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    target = Path(tempfile.mkdtemp(prefix="api-", dir=WORK_DIR)) / "store"
    shutil.copytree(STORE, target)
    return target


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not STORE.exists():
        raise RuntimeError(
            f"no store at {STORE}. Run: python scripts/seed_demo.py"
        )
    state.store = _working_copy()
    state.actor = GraphActor(state.store, readers=2)
    state.actor.open_readers()
    try:
        yield
    finally:
        if state.actor:
            state.actor.close()
        if state.store:
            shutil.rmtree(state.store.parent, ignore_errors=True)


app = FastAPI(title="RIPPLE", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def actor() -> GraphActor:
    if state.actor is None:
        raise HTTPException(503, "store not ready")
    return state.actor


class PropagateRequest(BaseModel):
    retracted: bool = True
    work_id: str = FOCAL


@app.get("/api/scenario")
def scenario() -> dict[str, Any]:
    """The setup: which work, when it was retracted, and its citation reach."""
    a = actor()
    event = retraction_event(a, FOCAL)
    if event is None:
        raise HTTPException(404, f"no retraction event for {FOCAL}")

    exposure = post_retraction_breakdown(a, FOCAL)
    late = a.read(
        "MATCH (c:Work)-[r:CITES]->(:Work {openalex_id: $id}) "
        "WHERE r.awareness = 'unambiguous' "
        "RETURN c.openalex_id AS id, c.title AS title, c.type AS type, "
        "       c.pub_date AS published, r.post_retraction_lag_days AS lag "
        "ORDER BY lag DESC LIMIT 10",
        {"id": FOCAL},
    )
    also_retracted = a.read(
        "MATCH (d:Work {openalex_id: $d})-[:CITES]->(ref:Work) "
        "WHERE ref.is_retracted = true "
        "RETURN ref.openalex_id AS id, ref.title AS title, ref.doi AS doi "
        "ORDER BY ref.cited_by_count DESC",
        {"d": "W3216235612"},
    )
    totals = a.read(
        "MATCH (n) RETURN count(n) AS nodes"
    )[0]
    rels = a.read("MATCH ()-[r]->() RETURN count(r) AS rels")[0]

    return {
        "focal": event,
        "exposure": exposure,
        "late_citations": late,
        "guidance": {"work_id": "W3216235612", "also_retracted": also_retracted},
        "store": {"nodes": totals["nodes"], "relationships": rels["rels"]},
        "status_meta": STATUS_META,
    }


@app.post("/api/propagate")
def do_propagate(req: PropagateRequest) -> dict[str, Any]:
    """Re-derive every claim, with the work either standing or retracted."""
    a = actor()
    if req.retracted:
        result = propagate(a, retracted_work=req.work_id, epoch=2)
    else:
        a.write(
            "MATCH (w:Work {openalex_id:$id}) SET w.is_retracted = false, w.label = 'IN'",
            {"id": req.work_id},
        )
        result = propagate(a, epoch=1, write_back=True)

    verdicts = [
        {
            "belief_id": v.belief_id,
            "status": v.status.value,
            "reason_code": v.reason_code,
            "witness": v.witness_justification_id,
            "witness_chain": v.witness_chain,
            "killers": v.killers,
            "missing": v.missing_antecedents,
            "live_support": v.live_support_count,
            "prior_support": v.prior_support_count,
            "holds": v.status in HOLDS,
        }
        for v in result.verdicts
    ]
    return {
        "retracted": req.retracted,
        "epoch": result.epoch,
        "summary": result.summary,
        "verdicts": verdicts,
        "n_beliefs": result.n_beliefs,
        "n_justifications": result.n_justifications,
        "graph": _graph_payload(a, {v["belief_id"]: v["status"] for v in verdicts}),
    }


def _graph_payload(a: GraphActor, statuses: dict[str, str]) -> dict[str, Any]:
    """The justification layer, shaped for Cytoscape.

    Justification nodes are kept rather than collapsed. They are where the
    threshold lives, so showing them is what makes "2 of 3 survived" visible
    instead of an unexplained colour.
    """
    claims = a.read(
        "MATCH (c:Claim) RETURN c.id AS id, c.text AS text, "
        "c.provenance_tier AS tier, c.label AS label ORDER BY id"
    )
    justs = a.read(
        "MATCH (j:Justification)-[:SUPPORTS]->(c:Claim) "
        "RETURN j.id AS id, j.threshold AS threshold, j.arity AS arity, "
        "       j.rule AS rule, j.confidence AS confidence, "
        "       j.provenance_tier AS tier, c.id AS consequent ORDER BY j.id"
    )
    antecedents = a.read(
        "MATCH (b)-[:ANTECEDENT_OF]->(j:Justification) "
        "RETURN coalesce(b.id, b.openalex_id) AS belief, head(labels(b)) AS kind, "
        "       coalesce(b.text, b.title) AS title, "
        "       coalesce(b.is_retracted, false) AS retracted, "
        "       b.label AS label, b.doi AS doi, j.id AS justification "
        "ORDER BY j.id, belief"
    )

    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()

    for c in claims:
        nodes.append({
            "data": {
                "id": c["id"], "kind": "claim", "label": c["id"],
                "text": c["text"], "tier": c["tier"],
                "status": statuses.get(c["id"], Status.UNAFFECTED.value),
            }
        })
        seen.add(c["id"])

    for j in justs:
        nodes.append({
            "data": {
                "id": j["id"], "kind": "justification",
                "label": f"{j['threshold']} of {j['arity']}",
                "threshold": j["threshold"], "arity": j["arity"],
                "rule": j["rule"], "confidence": j["confidence"], "tier": j["tier"],
            }
        })
        seen.add(j["id"])

    edges: list[dict[str, Any]] = []
    for row in antecedents:
        bid = row["belief"]
        if bid not in seen:
            nodes.append({
                "data": {
                    "id": bid,
                    "kind": "work" if row["kind"] == "Work" else "claim",
                    "label": bid,
                    "text": row["title"],
                    "doi": row["doi"],
                    "retracted": bool(row["retracted"]),
                    "status": statuses.get(
                        bid,
                        Status.RETRACTED.value if row["retracted"] else Status.SUPPORTED.value,
                    ),
                }
            })
            seen.add(bid)
        edges.append({
            "data": {
                "id": f"a_{bid}_{row['justification']}",
                "source": bid, "target": row["justification"], "kind": "antecedent",
                "dead": bool(row["retracted"]) or statuses.get(bid) in {
                    Status.UNSUPPORTED.value, Status.UNSUPPORTED_CIRCULAR.value
                },
            }
        })

    for j in justs:
        edges.append({
            "data": {
                "id": f"s_{j['id']}_{j['consequent']}",
                "source": j["id"], "target": j["consequent"], "kind": "supports",
            }
        })

    return {"nodes": nodes, "edges": edges}


@app.get("/api/claims/{claim_id}/support")
def support(claim_id: str) -> dict[str, Any]:
    """Every justification for a claim, with its antecedents and provenance."""
    rows = actor().read(Q_SUPPORT_SET, {"claim_id": claim_id})
    if not rows:
        raise HTTPException(404, f"no justifications for {claim_id}")
    return {"claim_id": claim_id, "justifications": rows}


@app.get("/api/exposure")
def exposure(depth: int = 1) -> dict[str, Any]:
    """Citation reach, which is exposure and not loss of support."""
    try:
        query = q_blast_radius(depth)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"depth": depth, "rows": actor().read(query, {"work_id": FOCAL})}


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": state.actor is not None, "store": str(state.store)}
