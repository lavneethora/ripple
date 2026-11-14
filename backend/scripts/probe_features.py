"""Probe which Cypher features Relate actually supports.

RIPPLE's design deliberately avoids several Cypher constructs whose support is
undocumented. This script replaces guesswork with a committed compatibility
record: run it, read the matrix, and design against what actually works.

Finding a gap in week 1 is a shrug. Finding one in week 4 is fatal.

Usage:
    python backend/scripts/probe_features.py
"""

import sys
import traceback

import relate

FIXTURE = [
    "CREATE (a:Probe {id: 'a', n: 1, tag: 'x'})",
    "CREATE (b:Probe {id: 'b', n: 2, tag: 'y'})",
    "CREATE (c:Probe {id: 'c', n: 3, tag: 'x'})",
    "MATCH (a:Probe {id:'a'}), (b:Probe {id:'b'}) CREATE (a)-[:LINK {w: 10}]->(b)",
    "MATCH (b:Probe {id:'b'}), (c:Probe {id:'c'}) CREATE (b)-[:LINK {w: 20}]->(c)",
    # A deliberate 2-cycle: the citation graph has these, and several probes
    # exist specifically to check that traversal survives them.
    "MATCH (c:Probe {id:'c'}), (a:Probe {id:'a'}) CREATE (c)-[:LINK {w: 30}]->(a)",
]

# (category, name, cypher, params)
PROBES = [
    # --- control: if these fail, the probe harness itself is wrong ---
    ("baseline", "MATCH + RETURN", "MATCH (n:Probe) RETURN count(n) AS n", {}),
    ("baseline", "parameters", "MATCH (n:Probe {id: $id}) RETURN n.n AS n", {"id": "a"}),

    # --- branching: CASE is the big unknown; these are the fallbacks ---
    ("branching", "CASE expression",
     "MATCH (n:Probe) RETURN CASE WHEN n.n > 1 THEN 'hi' ELSE 'lo' END AS b", {}),
    ("branching", "size([x IN list WHERE ...])  [CASE fallback]",
     "MATCH (n:Probe) WITH collect(n.n) AS ns RETURN size([x IN ns WHERE x > 1]) AS c", {}),
    ("branching", "UNION of two WHERE arms  [CASE fallback]",
     "MATCH (n:Probe) WHERE n.n > 1 RETURN n.id AS id, 'hi' AS b "
     "UNION MATCH (n:Probe) WHERE n.n <= 1 RETURN n.id AS id, 'lo' AS b", {}),
    ("branching", "coalesce()",
     "MATCH (n:Probe) RETURN coalesce(n.missing, 'dflt') AS v LIMIT 1", {}),

    # --- traversal: the engine's hot path depends on these ---
    ("traversal", "variable-length *1..3",
     "MATCH (a:Probe {id:'a'})-[:LINK*1..3]->(x) RETURN count(DISTINCT x) AS n", {}),
    ("traversal", "quantified path pattern {1,3}",
     "MATCH (a:Probe {id:'a'}) ((p)-[:LINK]->(q)){1,3} (x) RETURN count(DISTINCT x) AS n", {}),
    ("traversal", "QPP with PARAM bound {1,$d}  [expected FAIL]",
     "MATCH (a:Probe {id:'a'}) ((p)-[:LINK]->(q)){1,$d} (x) RETURN count(x) AS n", {"d": 3}),
    ("traversal", "var-length with PARAM bound *1..$d  [expected FAIL]",
     "MATCH (a:Probe {id:'a'})-[:LINK*1..$d]->(x) RETURN count(x) AS n", {"d": 3}),
    ("traversal", "named path + length()",
     "MATCH p = (a:Probe {id:'a'})-[:LINK*1..2]->(x) RETURN min(length(p)) AS hops", {}),
    ("traversal", "shortestPath (legacy form)",
     "MATCH (a:Probe {id:'a'}), (c:Probe {id:'c'}) "
     "MATCH p = shortestPath((a)-[:LINK*1..5]->(c)) RETURN length(p) AS hops", {}),
    ("traversal", "SHORTEST 1 path selector",
     "MATCH p = SHORTEST 1 (a:Probe {id:'a'})-[:LINK*1..5]->(c:Probe {id:'c'}) "
     "RETURN length(p) AS hops", {}),
    ("traversal", "cycle survival (unbounded-ish *1..6 on a 3-cycle)",
     "MATCH (a:Probe {id:'a'})-[:LINK*1..6]->(x) RETURN count(*) AS paths", {}),

    # --- subquery expressions: used throughout queries.py ---
    ("subquery", "COUNT { }",
     "MATCH (n:Probe) RETURN n.id AS id, COUNT { MATCH (n)-[:LINK]->() } AS deg", {}),
    ("subquery", "EXISTS { }",
     "MATCH (n:Probe) WHERE EXISTS { MATCH (n)-[:LINK]->() } RETURN count(n) AS n", {}),
    ("subquery", "COLLECT { }",
     "MATCH (n:Probe) RETURN COLLECT { MATCH (n)-[:LINK]->(m) RETURN m.id } AS outs", {}),
    ("subquery", "CALL ( ) { } scoped subquery",
     "MATCH (n:Probe) CALL (n) { MATCH (n)-[:LINK]->(m) RETURN m.id AS mid } "
     "RETURN n.id AS id, mid", {}),
    ("subquery", "NOT EXISTS { } with inner WHERE",
     "MATCH (n:Probe) WHERE NOT EXISTS { MATCH (n)-[:LINK]->(m) WHERE m.n > 99 } "
     "RETURN count(n) AS n", {}),

    # --- writes ---
    ("write", "MERGE node",
     "MERGE (p:ProbeMerge {id: 'm1'}) RETURN p.id AS id", {}),
    ("write", "MERGE relationship",
     "MATCH (a:Probe {id:'a'}), (c:Probe {id:'c'}) MERGE (a)-[r:MERGED]->(c) RETURN type(r) AS t", {}),
    ("write", "UNWIND + SET batch writeback",
     "UNWIND $rows AS row MATCH (n:Probe {id: row.id}) SET n.label = row.label "
     "RETURN count(n) AS updated", {"rows": [{"id": "a", "label": "IN"}, {"id": "b", "label": "OUT"}]}),

    # --- lists, maps, aggregation ---
    ("data", "list comprehension",
     "MATCH (n:Probe) WITH collect(n.n) AS ns RETURN [x IN ns WHERE x > 1 | x * 2] AS doubled", {}),
    ("data", "map in collect()",
     "MATCH (n:Probe) RETURN collect({id: n.id, n: n.n}) AS rows", {}),
    ("data", "GROUP BY",
     "MATCH (n:Probe) RETURN n.tag AS tag, count(*) AS c GROUP BY tag", {}),
    ("data", "implicit grouping (no GROUP BY)",
     "MATCH (n:Probe) RETURN n.tag AS tag, count(*) AS c", {}),
    ("data", "labels() + head()",
     "MATCH (n:Probe) RETURN head(labels(n)) AS lbl LIMIT 1", {}),
    ("data", "elementId()",
     "MATCH (n:Probe) RETURN elementId(n) AS eid LIMIT 1", {}),
    ("data", "startNode() / endNode()",
     "MATCH ()-[r:LINK]->() RETURN startNode(r).id AS s, endNode(r).id AS e LIMIT 1", {}),
    ("data", "IS NULL on OPTIONAL MATCH",
     "MATCH (n:Probe) OPTIONAL MATCH (n)-[:NOPE]->(m) RETURN count(*) AS n, m IS NULL AS absent", {}),
    ("data", "integer arithmetic (epoch-day model)",
     "RETURN 18444 - 18000 AS lag, 18444 > 18000 AS after", {}),

    # --- dates: avoided by design, probed to confirm the decision ---
    ("dates", "date() function", "RETURN date('2020-07-01') AS d", {}),
    # TRAP: .days on duration.between() is the DAY COMPONENT of a Y/M/D
    # breakdown, not the total. 2020-07-01 -> 2021-09-13 is 14 months + 12 days,
    # so .days returns 12 where the answer is 439. A "18 months after the
    # retraction" filter written the obvious way silently returns nonsense.
    # This is why RIPPLE stores epoch-days as integers.
    ("dates", "duration.between().days  [TRAP: returns 12, truth is 439]",
     "RETURN duration.between(date('2020-07-01'), date('2021-09-13')).days AS days", {}),
    ("dates", "duration.inDays().days  [correct: 439]",
     "RETURN duration.inDays(date('2020-07-01'), date('2021-09-13')).days AS days", {}),
    ("dates", "epoch-day subtraction  [correct: 439, and what RIPPLE uses]",
     "RETURN 18883 - 18444 AS days", {}),

    # --- schema ---
    ("schema", "CREATE INDEX (range)",
     "CREATE INDEX probe_id_idx IF NOT EXISTS FOR (n:Probe) ON (n.id)", {}),
    ("schema", "CREATE CONSTRAINT (unique)",
     "CREATE CONSTRAINT probe_uq IF NOT EXISTS FOR (n:ProbeC) REQUIRE n.cid IS UNIQUE", {}),
    ("schema", "SHOW CONSTRAINTS", "SHOW CONSTRAINTS", {}),
    ("schema", "SHOW INDEXES", "SHOW INDEXES", {}),
    ("schema", "CREATE FULLTEXT INDEX",
     "CREATE FULLTEXT INDEX probe_ft IF NOT EXISTS FOR (n:Probe) ON EACH [n.tag]", {}),

    # --- confirm absent, so the README can say so with evidence ---
    ("absent", "APOC  [expected FAIL]",
     "RETURN apoc.text.join(['a','b'], '-') AS s", {}),
    ("absent", "GDS  [expected FAIL]",
     "CALL gds.pageRank.stream('g') YIELD nodeId RETURN nodeId", {}),
]


def main() -> int:
    conn = relate.connect(":memory:")
    for stmt in FIXTURE:
        conn.execute(stmt)

    results = []
    for category, name, cypher, params in PROBES:
        try:
            cur = conn.execute(cypher, params) if params else conn.execute(cypher)
            rows = cur.fetchall()
            sample = ""
            if rows:
                # Record is dict-like and iterates as KEYS, not values;
                # integer indexing raises KeyError. dict() materializes it.
                sample = str(dict(rows[0]))[:70]
            results.append((category, name, "PASS", sample))
        except Exception as exc:  # noqa: BLE001 - probing, every failure is data
            detail = f"{type(exc).__name__}: {exc}"
            results.append((category, name, "FAIL", detail.replace("\n", " ")[:110]))

    width = max(len(n) for _, n, _, _ in results)
    current = None
    n_pass = 0
    for category, name, status, detail in results:
        if category != current:
            print(f"\n## {category}")
            current = category
        if status == "PASS":
            n_pass += 1
        print(f"  [{status}] {name.ljust(width)}  {detail}")

    print(f"\n{n_pass}/{len(results)} probes passed.")
    print("Probes marked [expected FAIL] failing is the correct outcome.")
    conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
