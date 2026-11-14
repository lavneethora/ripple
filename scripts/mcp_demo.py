"""Show what an agent sees when it asks RIPPLE what still holds.

    python scripts/mcp_demo.py

Speaks MCP over stdio to backend/ripple/mcp_server.py, exactly as a coding
agent would.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def unwrap(result) -> dict:
    structured = getattr(result, "structured_content", None)
    return structured if structured else json.loads(result.content[0].text)


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "backend" / "ripple" / "mcp_server.py")],
        cwd=str(ROOT),
        env={"PYTHONPATH": str(ROOT / "backend")},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            print("=" * 72)
            print(f"  connected to MCP server: {info.server_info.name}")
            print("=" * 72)

            tools = (await session.list_tools()).tools
            print(f"\n{len(tools)} tools offered:")
            for t in tools:
                print(f"  {t.name}")

            print("\n--- agent asks: what is the scenario? ---")
            s = unwrap(await session.call_tool("scenario"))
            w = s["retracted_work"]
            print(f"  {w['title'][:62]}")
            print(f"  retracted {w['retracted_on']}, reason {w['reason_class']}")
            for row in s["exposure_by_awareness"]:
                print(f"  exposure: {row['awareness']:<20} n={row['n']}")
            print(f"  note: {s['exposure_note'][:72]}...")

            print("\n--- agent asks: what still holds? ---")
            r = unwrap(await session.call_tool("what_still_holds", {"retracted": True}))
            for c in sorted(r["claims"], key=lambda c: c["status"]):
                print(f"  {c['status']:<14} {c['claim_id']}")
            print(f"  summary: {r['summary']}")

            print("\n--- agent asks: why does the recommendation survive? ---")
            e = unwrap(await session.call_tool(
                "explain_claim", {"claim_id": "CL_HCQ_NO_MORTALITY_BENEFIT"}))
            for j in e["justifications"]:
                print(f"  {j['rule']}: {j['live_antecedents']}/{len(j['antecedents'])} live, "
                      f"needs {j['threshold']}, satisfied={j['satisfied']} "
                      f"[{j['provenance_tier']}]")
                for a in j["antecedents"]:
                    mark = "x" if a["retracted"] else "+"
                    print(f"    {mark} {a['belief_id']}  {(a['title'] or '')[:52]}")

            print("\n--- agent asks: is this notice substantive? ---")
            n = unwrap(await session.call_tool("classify_retraction_notice", {
                "notice_text": "The Publisher regrets that this article is an "
                               "accidental duplication of an article that has "
                               "already been published."}))
            print(f"  reason_class      : {n['reason_class']}")
            print(f"  impeaches_findings: {n['impeaches_findings']}")
    print("\n" + "=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
