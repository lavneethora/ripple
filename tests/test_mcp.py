"""The MCP surface, exercised over a real stdio session.

Importing the module and calling the functions would prove almost nothing:
the point of this surface is that an agent can reach it over the protocol,
so the tests spawn the server as a subprocess and speak MCP to it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "backend" / "ripple" / "mcp_server.py")],
        cwd=str(ROOT),
        env={"PYTHONPATH": str(ROOT / "backend")},
    )


def payload(result) -> dict:
    """Unwrap a CallToolResult into the dict the tool returned."""
    assert not result.is_error, result.content
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured
    return json.loads(result.content[0].text)


async def call(name: str, args: dict | None = None) -> dict:
    async with stdio_client(params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return payload(await session.call_tool(name, args or {}))


async def test_server_advertises_its_tools():
    async with stdio_client(params()) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            assert info.server_info.name == "ripple"
            names = {t.name for t in (await session.list_tools()).tools}
            assert names == {
                "scenario",
                "what_still_holds",
                "explain_claim",
                "post_retraction_citations",
                "classify_retraction_notice",
                "list_claims",
            }


async def test_instructions_warn_against_the_two_misreadings():
    """An agent reading these must not conflate exposure with loss of support."""
    async with stdio_client(params()) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            text = (info.instructions or "").lower()
            assert "citation is not a justification" in text
            assert "procedural" in text


async def test_scenario_reports_the_real_retraction():
    data = await call("scenario")
    assert data["retracted_work"]["retracted_on"] == "2020-07-01"
    assert data["retracted_work"]["reason_class"] == "SUBSTANTIVE"
    assert data["store"]["claims"] == 6
    # Exposure must arrive labelled as exposure.
    assert "not" in data["exposure_note"].lower()


async def test_what_still_holds_returns_the_curated_verdicts():
    data = await call("what_still_holds", {"retracted": True})
    statuses = {c["claim_id"]: c["status"] for c in data["claims"]}
    assert statuses["CL_HCQ_TREATS_COVID"] == "UNSUPPORTED"
    assert statuses["CL_HCQ_NOT_INDICATED"] == "SUPPORTED"
    assert statuses["CL_COVID_HEART_BLOCK_CHILD"] == "QUESTIONED"
    assert data["summary"] == {"SUPPORTED": 4, "QUESTIONED": 1, "UNSUPPORTED": 1}


async def test_what_still_holds_can_show_the_prior_state():
    data = await call("what_still_holds", {"retracted": False})
    statuses = {c["claim_id"]: c["status"] for c in data["claims"]}
    assert statuses["CL_HCQ_TREATS_COVID"] == "SUPPORTED"


async def test_explain_claim_shows_the_threshold_and_antecedents():
    data = await call("explain_claim", {"claim_id": "CL_HCQ_NO_MORTALITY_BENEFIT"})
    j = data["justifications"][0]
    assert j["threshold"] == 2
    assert len(j["antecedents"]) == 3
    assert j["satisfied"] is True
    assert j["provenance_tier"] == "curated"


async def test_explain_claim_lists_known_claims_when_asked_for_a_bad_one():
    data = await call("explain_claim", {"claim_id": "NOPE"})
    assert "error" in data
    assert "CL_HCQ_NOT_INDICATED" in data["known_claims"]


async def test_post_retraction_citations_excludes_the_grace_window():
    data = await call("post_retraction_citations", {"limit": 20})
    assert data["citations"], "expected at least one late citation"
    assert all(c["lag_days"] > 180 for c in data["citations"])
    assert max(c["lag_days"] for c in data["citations"]) == 439


async def test_notice_classifier_is_available_to_agents():
    procedural = await call(
        "classify_retraction_notice",
        {"notice_text": "This article is an accidental duplication of an "
                        "article that has already been published."},
    )
    assert procedural["reason_class"] == "PROCEDURAL"
    assert procedural["impeaches_findings"] is False

    unknown = await call(
        "classify_retraction_notice", {"notice_text": "This article has been retracted."}
    )
    assert unknown["reason_class"] == "UNKNOWN"
    assert unknown["impeaches_findings"] is False


async def test_list_claims_reports_provenance():
    data = await call("list_claims")
    assert len(data["claims"]) == 6
    assert {c["provenance_tier"] for c in data["claims"]} == {"curated"}
