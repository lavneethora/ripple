# Demo video script

Target 1:55, hard cap 2:00 (Devpost allows up to 2 minutes).
Narration is 292 words, which is about 117 seconds at 150 words a minute, so
it only fits with the two marked pauses kept short. If a take runs long, the
line to cut is the one about citation distance at 1:06.

Record at 1512x900 or wider. Both servers running, browser at
`http://localhost:5180`, toggle set to **Study stands** before you hit record.

---

## 0:00 to 0:12 | The problem

**On screen:** the app, already loaded, toggle on "Study stands". Do not move
the mouse yet.

> When a paper gets retracted, nothing built on it changes. Nobody goes back
> to work out which conclusions were resting on it.

**On screen:** cursor moves to the header strip and rests there while the
retraction date and citation count are read.

> This is the hydroxychloroquine study from March 2020. Retracted that July,
> cited more than two thousand times since.

---

## 0:12 to 0:30 | What the graph is

**On screen:** slow cursor pass over one claim box, then over a diamond, then
over the `2 of 3` diamond under "No mortality benefit". Do not click.

> RIPPLE loads real papers from OpenAlex and real retraction dates from
> Crossref into Relate, an embedded graph database. The boxes are
> conclusions. The diamonds are justifications, and the number on one is its
> threshold. Two of three means any two of those papers are enough.

---

## 0:30 to 0:44 | The retraction

**On screen:** click **Study retracted**. Let the flash land before speaking
again. This is the one beat that needs a real pause.

> Right now the study still stands. Watch what happens when I tell RIPPLE it
> was retracted.

*(pause about 1.5 seconds while the nodes flash)*

> One conclusion loses support, the claim that hydroxychloroquine treats
> COVID. That is the only one.

---

## 0:44 to 1:06 | The part that matters

**On screen:** cursor to "Do not use HCQ for COVID-19" at the top of the
graph, still green. Then click it. The derivation and the three trials appear
in the side panel.

> The conclusion at the top, do not use hydroxychloroquine for COVID, is
> untouched. It sits further downstream than the one that died, and it never
> rested on the retracted paper. It rests on three randomised trials that
> found no benefit, and it needs any two of them.

*(short pause)*

> Anything propagating by citation distance would have flagged it.

---

## 1:06 to 1:18 | Exposure is not support

**On screen:** cursor to the "Exposure is not loss of support" panel, resting
on the two numbers as they are read.

> More than five thousand papers cite this study, but citing something is not
> the same as resting on it, so RIPPLE reports those as two different
> numbers.

---

## 1:18 to 1:36 | The flags that do not hold up

**On screen:** click the amber "COVID can cause heart block in a child" claim,
then click its withdrawn-duplicate antecedent so the work panel shows the
reason.

> Retractions are the same. OpenAlex flags three of this document's
> references as retracted. Only one of those should change what you believe.
> One was withdrawn as an accidental duplicate, which says nothing about
> whether the finding is right, and one has no retraction record in Crossref
> at all.

---

## 1:36 to 1:50 | Relate, and the agent surface

**On screen:** cut to a terminal. Run the read-only query, let the node count
print, then run `scripts/mcp_demo.py` and let the tool list and verdicts
scroll.

> Relate does the traversal. The store ships in the repo, so you can query it
> with Cypher without running the app.

> The same engine runs over MCP, so an agent can ask what still holds.

---

## 1:50 to 1:55 | Close

**On screen:** `pytest -q` finishing on 114 passed, or the repo page with the
`relate-hackathon` topic visible.

> Clone it, run the tests, and you get these numbers.

---

## Terminal commands, in order

Have these typed and ready so there is no live typing on camera.

```bash
./.venv/bin/python -c "import relate; c=relate.connect('data/ripple-retractions', read_only=True); print(dict(c.execute('MATCH (n) RETURN count(n) AS n').fetchall()[0]))"
```

```bash
./.venv/bin/python scripts/mcp_demo.py
```

```bash
./.venv/bin/python -m pytest -q
```

## Every figure spoken, and where it comes from

Check these against the running app before recording. If the store is
reseeded they can move.

| Said in narration | Value | Source |
|---|---|---|
| retracted that July | 2020-07-01 | Crossref `update-to` |
| cited more than two thousand times since | 2,348 citing works published 2021 or later | OpenAlex `cites:` grouped by year |
| more than five thousand papers cite this study | 5,408 | OpenAlex `cited_by_count` |
| three randomised trials, needs any two | threshold 2, arity 3 | `J_HCQ_NO_MORTALITY_RCTS` |
| three flagged, one should change what you believe | 1 substantive, 1 procedural, 1 disputed flag | Crossref, per reference |
| 114 passed | test count | `pytest -q` |

## Production checklist

- Under 2:00. Devpost rejects longer.
- Your own filming and editing. No third party music or stock footage unless
  you have permission.
- Viewable without a login. Unlisted YouTube or Vimeo is fine, Google Drive
  with link access is risky.
- Record with the API and web servers already warm. The first propagate call
  after a cold start copies the store and takes a second.
- Set the toggle back to "Study stands" before each take.
- Do not say "three retracted works" anywhere. Two of the three are
  retractions in the record and only one of those impeaches a finding, so the
  script is written to say that instead.
