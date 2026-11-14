"""Every Cypher statement RIPPLE runs, as named constants.

Two rules shape this file.

Never loop a query. Cypher has no recursive fixpoint and no WHILE, so the
engine pulls the affected region in one read and iterates in Python. The
tempting alternative, one variable-length query returning paths, is
exponential over a cyclic citation graph and the hop cap does not save it.

Never parameterise a traversal bound. Both `{1,$d}` and `*1..$d` are rejected
by this engine, so depth is templated from a validated int by `expand_depth`.
See docs/cypher-compatibility.md.
"""

from __future__ import annotations

from typing import Final

MAX_EXPAND_DEPTH: Final[int] = 6


def expand_depth(depth: int) -> int:
    """Validate a traversal depth before it is templated into a query string.

    Depth cannot be a query parameter, so it reaches Cypher by string
    formatting. This is the only gate between that and an injection.
    """
    if not isinstance(depth, int) or isinstance(depth, bool):
        raise TypeError(f"depth must be an int, got {type(depth).__name__}")
    if not 1 <= depth <= MAX_EXPAND_DEPTH:
        raise ValueError(f"depth {depth} outside 1..{MAX_EXPAND_DEPTH}")
    return depth


# --- justification layer: what the engine reasons over --------------------

# One hop of derivation, set-valued rather than path-valued. Called at most
# MAX_EXPAND_DEPTH times to build the affected closure.
Q_EXPAND_ONE_LEVEL: Final[str] = """
UNWIND $ids AS bid
MATCH (b)-[:ANTECEDENT_OF]->(j:Justification)-[:SUPPORTS]->(c:Claim)
WHERE coalesce(b.id, b.openalex_id) = bid
RETURN DISTINCT c.id AS consequent_id, j.id AS via_justification
"""

# The single read that feeds the Python fixpoint.
Q_PULL_PROJECTION: Final[str] = """
MATCH (j:Justification)-[:SUPPORTS]->(c:Claim)
WHERE c.id IN $scope_ids
MATCH (a)-[ae:ANTECEDENT_OF]->(j)
OPTIONAL MATCH (a)-[:HAS_RETRACTION]->(re:RetractionEvent)
WITH j, c, collect({
        id: coalesce(a.id, a.openalex_id),
        kind: head(labels(a)),
        role: ae.role,
        label: a.label,
        retracted: coalesce(a.is_retracted, false),
        reason_class: re.reason_class
     }) AS antecedents
RETURN j.id              AS jid,
       j.threshold       AS threshold,
       j.rule            AS rule,
       j.confidence      AS confidence,
       j.provenance_tier AS tier,
       c.id              AS consequent,
       antecedents,
       size(antecedents) AS arity
ORDER BY jid
"""

# Every claim in the store, for a full relabel rather than an incremental one.
Q_ALL_CLAIM_IDS: Final[str] = """
MATCH (c:Claim) RETURN c.id AS id ORDER BY id
"""

Q_SUPPORT_SET: Final[str] = """
MATCH (j:Justification)-[:SUPPORTS]->(c:Claim {id: $claim_id})
MATCH (a)-[ae:ANTECEDENT_OF]->(j)
OPTIONAL MATCH (a)<-[:REPORTS]-(src:Work)
WITH j, a, ae,
     collect(DISTINCT {
        work_id: src.openalex_id, doi: src.doi, title: src.title,
        retracted: src.is_retracted, pub_day: src.pub_day
     }) AS sources
WITH j, collect({
        belief_id: coalesce(a.id, a.openalex_id),
        kind: head(labels(a)),
        role: ae.role,
        title: coalesce(a.text, a.title),
        label: a.label,
        retracted: coalesce(a.is_retracted, false),
        sources: sources
     }) AS antecedents
RETURN j.id AS justification_id, j.rule AS rule, j.threshold AS threshold,
       j.confidence AS confidence, j.provenance_tier AS tier,
       antecedents,
       size([x IN antecedents WHERE x.label = 'IN'])  AS n_in,
       size([x IN antecedents WHERE x.label = 'OUT']) AS n_out,
       size([x IN antecedents WHERE x.retracted])     AS n_retracted
ORDER BY n_out, justification_id
"""

# Surviving support must clear three bars: threshold met on live antecedents,
# no antecedent inside the retraction's affected set, and author-disjoint from
# the retracted work, because shared authorship is correlated evidence rather
# than independent corroboration.
Q_SURVIVING_CORROBORATION: Final[str] = """
MATCH (r:Work {openalex_id: $work_id})-[:AUTHORED_BY]->(ra:Author)
WITH collect(DISTINCT ra.openalex_id) AS retracted_authors

MATCH (j:Justification)-[:SUPPORTS]->(c:Claim)
WHERE c.id IN $affected_claim_ids
  AND NOT EXISTS {
        MATCH (bad)-[:ANTECEDENT_OF]->(j)
        WHERE coalesce(bad.id, bad.openalex_id) IN $affected_belief_ids
      }
  AND NOT EXISTS {
        MATCH (dead)-[:ANTECEDENT_OF]->(j) WHERE dead.label = 'OUT'
      }
WITH retracted_authors, c, j,
     COUNT { MATCH (live)-[:ANTECEDENT_OF]->(j) WHERE live.label = 'IN' } AS n_live
WHERE n_live >= j.threshold

MATCH (ant)-[:ANTECEDENT_OF]->(j)
OPTIONAL MATCH (ant)<-[:REPORTS]-(w:Work)-[:AUTHORED_BY]->(au:Author)
WITH c, j, n_live, retracted_authors,
     collect(DISTINCT w.openalex_id)  AS support_works,
     collect(DISTINCT au.openalex_id) AS support_authors
WITH c, j, n_live, support_works,
     size([x IN support_authors WHERE x IN retracted_authors]) AS author_overlap
WHERE author_overlap = 0
RETURN c.id AS claim_id, j.id AS surviving_justification, j.rule AS rule,
       j.confidence AS confidence, n_live, j.threshold AS threshold, support_works
ORDER BY j.confidence DESC, surviving_justification
"""

Q_WRITEBACK_STATUS: Final[str] = """
UNWIND $rows AS row
MATCH (c:Claim {id: row.claim_id})
SET c.label        = row.label,
    c.status       = row.status,
    c.reason_code  = row.reason_code,
    c.status_epoch = row.epoch
RETURN count(c) AS n
"""

# --- bibliographic layer: exposure and display only -----------------------

def q_blast_radius(depth: int) -> str:
    """Citation reach. Large, and epistemically meaningless on its own.

    Reported next to the justification result precisely so the difference
    between exposure and loss of support is visible.
    """
    d = expand_depth(depth)
    return f"""
MATCH (r:Work {{openalex_id: $work_id}})
MATCH p = (r) ( (x:Work)<-[:CITES]-(y:Work) ){{1,{d}}} (citing:Work)
WITH citing, min(length(p)) AS hops
RETURN hops, count(citing) AS n_works, collect(citing.openalex_id)[0..25] AS sample_ids
ORDER BY hops
"""


# The fast path: the post-retraction filter reduces to one comparison on a
# denormalized edge property, which is why that property is indexed.
Q_POST_RETRACTION_CITES: Final[str] = """
MATCH (citing:Work)-[cit:CITES]->(r:Work {openalex_id: $work_id})
WHERE cit.post_retraction_lag_days >= $min_lag_days
  AND cit.awareness = 'unambiguous'
RETURN citing.openalex_id AS work_id, citing.doi AS doi, citing.title AS title,
       citing.type AS work_type, citing.pub_date AS published,
       cit.post_retraction_lag_days AS lag_days
ORDER BY lag_days DESC
LIMIT $limit
"""

Q_POST_RETRACTION_BREAKDOWN: Final[str] = """
MATCH (citing:Work)-[cit:CITES]->(r:Work {openalex_id: $work_id})
WHERE cit.post_retraction_lag_days IS NOT NULL
RETURN cit.awareness AS awareness,
       count(cit) AS n,
       min(cit.post_retraction_lag_days) AS min_lag,
       max(cit.post_retraction_lag_days) AS max_lag
ORDER BY n DESC
"""

Q_RETRACTION_EVENT: Final[str] = """
MATCH (w:Work {openalex_id: $work_id})-[:HAS_RETRACTION]->(re:RetractionEvent)
RETURN re.id AS id, re.retracted_on AS retracted_on, re.valid_to_day AS valid_to_day,
       re.announced_day AS announced_day, re.notice_doi AS notice_doi,
       re.reason_class AS reason_class, re.source AS source,
       w.title AS title, w.doi AS doi, w.cited_by_count AS cited_by_count
"""

# --- writing the curated justification layer ------------------------------

Q_UPSERT_CLAIMS: Final[str] = """
UNWIND $rows AS row
MERGE (c:Claim {id: row.id})
SET c.text = row.text,
    c.kind = row.kind,
    c.provenance_tier = row.provenance_tier,
    c.label = row.label
RETURN count(c) AS n
"""

Q_UPSERT_REPORTS: Final[str] = """
UNWIND $rows AS row
MATCH (w:Work {openalex_id: row.work_id})
MATCH (c:Claim {id: row.claim_id})
MERGE (w)-[r:REPORTS]->(c)
SET r.section = row.section
RETURN count(r) AS n
"""

Q_UPSERT_JUSTIFICATIONS: Final[str] = """
UNWIND $rows AS row
MERGE (j:Justification {id: row.id})
SET j.rule = row.rule,
    j.threshold = row.threshold,
    j.arity = row.arity,
    j.confidence = row.confidence,
    j.provenance_tier = row.provenance_tier,
    j.created_day = row.created_day
WITH j, row
MATCH (c:Claim {id: row.consequent})
MERGE (j)-[:SUPPORTS]->(c)
RETURN count(j) AS n
"""

# Antecedents may be Works or Claims, and a single MATCH cannot span both
# labels cleanly, so the two are written in separate passes.
Q_UPSERT_ANTECEDENTS_WORK: Final[str] = """
UNWIND $rows AS row
MATCH (j:Justification {id: row.justification_id})
MATCH (w:Work {openalex_id: row.antecedent})
MERGE (w)-[r:ANTECEDENT_OF]->(j)
SET r.role = row.role
RETURN count(r) AS n
"""

Q_UPSERT_ANTECEDENTS_CLAIM: Final[str] = """
UNWIND $rows AS row
MATCH (j:Justification {id: row.justification_id})
MATCH (c:Claim {id: row.antecedent})
MERGE (c)-[r:ANTECEDENT_OF]->(j)
SET r.role = row.role
RETURN count(r) AS n
"""

Q_MARK_RETRACTED_OUT: Final[str] = """
MATCH (w:Work {openalex_id: $work_id})
SET w.label = 'OUT', w.is_retracted = true
RETURN w.openalex_id AS id, w.label AS label
"""
