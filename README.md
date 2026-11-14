# RIPPLE

Which of our conclusions still stand if this paper was retracted?

> **Status: in progress.** Built for the Relate by Neo4j Hackathon, started
> 2026-11-13, due 2026-12-11. The reasoning engine is complete and tested. The
> graph store is not seeded yet, so there is no demo to run today. See
> [Current state](#current-state) for exactly what works.

## Use case

A paper gets retracted. Everything built on top of it keeps standing, because
nobody recomputes what it was holding up.

This is not hypothetical. OpenAlex flags 144,580 retracted works. One of them,
the 2020 Gautret hydroxychloroquine paper, was retracted on 2020-07-01 and has
been cited roughly 2,350 times *since* that date, including by about 1,342
review articles. Reviews and clinical guidance documents are evidence
syntheses: other people's decisions rest on them.

RIPPLE answers the question that follows. Given a retraction, which downstream
conclusions actually lose their support, which survive on independent
corroboration, and which cannot be decided from what we know?

**Who benefits:** systematic reviewers checking whether an included study has
been withdrawn, journal editors assessing a submission's evidence base, and
meta-research on how retractions propagate.

### The relationships that matter

The governing rule is that **a citation is not a justification**. Running this
over citation edges would mean "connected to a retracted paper = broken",
which is wrong and is the exact failure the project exists to avoid. So there
are two layers over the same nodes:

| Layer | Edges | Meaning | Source |
|---|---|---|---|
| Bibliographic | `CITES`, `REPORTS`, `AUTHORED_BY` | who pointed at whom | OpenAlex, Crossref |
| Justification | `ANTECEDENT_OF` to `Justification` to `SUPPORTS` | what logically holds up what | curated, LLM-assisted |

Blast radius is measured on the first layer and is large. Status is computed
only on the second. Reporting both, and never confusing them, is the whole
claim to defensibility:

> 3,412 works cite this; 1,342 after the retraction. **27 conclusions actually
> lose support, 61 survive on independent corroboration, 9 are indeterminate.**

### Not every retraction means the work is wrong

Roughly a third of real retractions are procedural, and this dataset contains
both kinds plus a false positive:

| Work | OpenAlex | What the record shows |
|---|---|---|
| `W3010930696` Gautret 2020 | retracted | Retracted 2020-07-01. Reason curated from the publisher notice: **substantive**, so dependents lose support. |
| `W4205843467` | retracted | Withdrawn 2020-05-27 as an accidental duplicate. **Procedural**, so dependents are flagged, not broken. |
| `W3025220694` | retracted | **No Crossref retraction record.** The duplicate above was withdrawn; this original appears to have inherited the flag. Treated as not retracted. |

Crossref decides whether a retraction exists. Reasons are classified from the
notice where it is machine-readable, and otherwise curated by hand with a
citable source in `data/curated/retraction_reasons.yaml`. An unclassified
reason deliberately does **not** impeach the findings, because asserting a
conclusion is unsupported on the strength of a reason nobody has read is the
overreach this project exists to avoid.

### Honest caveat on the current data

The justification layer in this repository is **synthetic and labelled as
such**. Every claim is tagged `provenance_tier: synthetic` and prefixed
`SYN_`, and a test enforces that. OpenAlex records who cited whom but not
what rests on what, so this layer cannot be derived from the bibliographic
data, and inventing claims to attribute to a real clinical guidance document
would be the fabrication this project exists to detect.

The works, citations, retraction dates and lags are all real. The logical
structure over them is illustrative until curation against the source
documents replaces it.

### What is different about it

Relate's own cookbook can already record that a belief changed and query what
was believed when. What it does not do is **recompute what still holds**.
RIPPLE is a justification-based truth maintenance system: it re-derives every
downstream conclusion and emits a certificate showing which survived, on what
evidence, and which did not.

Status never depends on graph distance. A claim one hop from a retraction can
survive on 2-of-3 corroboration while one five hops away collapses, and that
inversion is asserted in the test suite rather than merely claimed.

## How it uses Relate

- **Mode:** directory store
- **Why this mode:** a directory store permits one writer alongside
  `read_only=True` readers, where a single-file `.rl` store refuses readers
  while a writer holds it. RIPPLE serves reads while propagating a retraction,
  so it needs both. It also lets judges inspect the store with the `relate`
  CLI without running the application.
- **What the graph models:** works, claims, reified justifications with
  AND/OR/m-of-n thresholds, retraction events, and bitemporal claim states.
  Cypher does the traversals and candidate discovery; the fixpoint labelling
  runs in Python, because Relate ships no APOC or GDS and Cypher has no
  recursive fixpoint. An LLM only extracts candidate claims and phrases
  explanations. It never decides a status.

### Relate capabilities used

Embedded Python client, directory store with read-only reader connections,
openCypher including quantified path patterns, `MERGE`, `CALL {}` subqueries,
`COUNT {}` / `EXISTS {}` / `COLLECT {}` subquery expressions, range and
fulltext indexes. An MCP tool surface is planned so an agent can query the
same engine.

A committed compatibility record of what the engine actually supports is at
[`docs/cypher-compatibility.md`](docs/cypher-compatibility.md), generated by a
probe script rather than assumed from documentation.

## Setup and run

Prerequisites: Python 3.9 or later. No API key, and no network access is
needed to run the tests.

```bash
git clone https://github.com/lavneethora/ripple.git
cd ripple
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
```

Confirm which Cypher features this Relate build supports:

```bash
./.venv/bin/python backend/scripts/probe_features.py
```

Run the test suite, including the truth maintenance correctness tests:

```bash
./.venv/bin/python -m pytest -q
```

See a real retraction re-derived end to end against the committed store:

```bash
./.venv/bin/python scripts/changing_mind_test.py
```

That prints what was believed while the study still stood, what the engine
derives once the retraction is known, and ten checks covering every branch of
the status lattice. It runs on a copy, so the committed store is unchanged.

### Run the app

Two servers. The API owns the Relate connection; the browser never touches
the store.

```bash
./.venv/bin/python -m uvicorn ripple.api.main:app --app-dir backend --port 8010
```

```bash
npm install --prefix frontend && npm run dev --prefix frontend
```

Then open http://localhost:5180 and toggle **Study stands** / **Study
retracted**. Claims are rectangles, works are smaller rectangles, and the
diamonds between them are justifications labelled with their threshold.
Click any claim to see what holds it up.

## Where the data lives

`data/ripple-retractions/` is the persistent Relate directory store,
committed to this repository so it can be inspected without running the
application. It currently holds 4,741 nodes and 5,900 relationships, built
entirely from real OpenAlex and Crossref records.

With only the Python package installed:

```bash
./.venv/bin/python -c "import relate; c=relate.connect('data/ripple-retractions', read_only=True); print(dict(c.execute('MATCH (n) RETURN count(n) AS n').fetchall()[0]))"
```

Or with the Relate CLI, which is a separate install from the Python package:

```bash
relate --database data/ripple-retractions --read-only "MATCH (n) RETURN count(n) AS n"
```

Rebuild it from source data with `python scripts/seed_demo.py`. HTTP
responses are cached under `data/raw/`, which is not committed, and every
write is a `MERGE`, so the script is safely re-runnable.

### What is in the store

A real clinical guidance document resting on retracted evidence:

- **Gautret et al 2020** (`W3010930696`), the hydroxychloroquine study,
  retracted 2020-07-01 per Crossref, cited 5,408 times.
- **ESC COVID-19 guidance** (`W3216235612`), published 2021-09-13, which
  cites it **439 days after the retraction** and is seeded with 309 of its
  references so surviving support can be evaluated against real evidence.
- Three of that document's references are flagged retracted by OpenAlex.
  **Only one of those holds up.** A second was withdrawn as an accidental
  duplicate, which says nothing about whether its findings are correct, and
  the third has no retraction record in Crossref at all. Saying "cites three
  retracted works" would overstate it threefold, so RIPPLE reports the
  breakdown and treats Crossref as authoritative.
- 30 further post-retraction citing reviews, classified by awareness:
  6 unambiguous, 23 within the in-press grace window, 2 with year-only dates
  that are deliberately excluded from headline counts.

## Current state

| Component | State |
|---|---|
| Cypher feature probe and compatibility record | working |
| `GraphActor` connection boundary | working, 9 tests |
| Truth maintenance engine | working, 27 tests |
| Graph schema and epoch-day model | working, 17 tests |
| OpenAlex and Crossref seeding | working |
| Seeded store in this repo | committed, 4,767 nodes |
| Engine wired to the store | working, 20 tests |
| HTTP API | working |
| Graph explorer UI | working |
| Justification layer | loader working; data is synthetic, curation pending |
| MCP tool surface | not started |

## Notes on scope and claims

RIPPLE reports that a conclusion cites a work retracted on a given date, which
is a checkable fact. It does not assert that any paper is wrong or that any
researcher acted improperly. Retractions are classified as substantive or
procedural, because roughly a third of real retractions are procedural
(plagiarism, authorship disputes, duplicate publication) and do not impeach
the findings. Those mark dependents as questioned rather than unsupported.

Every justification carries a provenance tier, shown next to any status it
produces, so hand-curated support is never presented as equivalent to
machine-extracted support.

## AI assistance

Built with AI coding assistance (Claude Code): implementation, tests,
tooling scripts, and research into what this build of Relate actually
supports.

## License

MIT. See [LICENSE](LICENSE).
