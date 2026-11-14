"""GraphActor must hold the thread-affinity invariant under concurrency.

Relate raises WrongThread only when a connection is actually touched from a
foreign thread, so a single-threaded test proves nothing. These tests hammer
the actor from many threads and from an asyncio event loop, which is how
FastAPI will drive it.
"""

from __future__ import annotations

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from ripple.graph.actor import GraphActor, WrongThreadError  # noqa: E402

N_CONCURRENT = 50


@pytest.fixture()
def actor(tmp_path: Path):
    store = tmp_path / "store"
    a = GraphActor(store, readers=2)
    a.write("CREATE (:Probe {id: 'seed', n: 1})")
    a.open_readers()
    yield a
    a.close()


def test_write_then_read_roundtrip(actor: GraphActor):
    actor.write("CREATE (:Probe {id: $id, n: $n})", {"id": "x", "n": 7})
    rows = actor.read("MATCH (p:Probe {id: $id}) RETURN p.n AS n", {"id": "x"})
    assert rows == [{"n": 7}]


def test_rows_are_plain_dicts(actor: GraphActor):
    # Record iterates as keys and rejects integer indexing, so the actor
    # normalizes to dict at the boundary. Everything downstream relies on this.
    rows = actor.read("MATCH (p:Probe) RETURN p.id AS id, p.n AS n LIMIT 1")
    assert isinstance(rows[0], dict)
    assert set(rows[0]) == {"id", "n"}


def test_read_sees_writes_committed_after_readers_opened(actor: GraphActor):
    """Regression guard for the frozen-snapshot hazard.

    A read-only Relate connection sees the store only as it was when it
    opened. Readers here are opened during fixture setup, so without
    generation-tracked refresh this read returns the pre-write count and the
    staleness is completely silent.
    """
    before = actor.read("MATCH (p:Probe) RETURN count(p) AS n")[0]["n"]
    actor.write("CREATE (:Probe {id: 'late', n: 1})")
    after = actor.read("MATCH (p:Probe) RETURN count(p) AS n")[0]["n"]
    assert after == before + 1


def test_interleaved_write_read_stays_consistent(actor: GraphActor):
    for i in range(5):
        actor.write("CREATE (:Probe {id: $id, n: 1})", {"id": f"seq{i}"})
        n = actor.read("MATCH (p:Probe) WHERE p.id STARTS WITH 'seq' RETURN count(p) AS n")
        assert n[0]["n"] == i + 1


def test_concurrent_reads_from_many_threads(actor: GraphActor):
    """50 concurrent reads across many OS threads must not raise WrongThread."""
    def one(i: int):
        return actor.read("MATCH (p:Probe) RETURN count(p) AS n")

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(one, range(N_CONCURRENT)))

    assert len(results) == N_CONCURRENT
    assert all(r[0]["n"] >= 1 for r in results)


def test_concurrent_writes_from_many_threads(actor: GraphActor):
    """Concurrent writes are serialized onto the single writer thread."""
    def one(i: int):
        return actor.write("CREATE (:Probe {id: $id, n: $n})", {"id": f"w{i}", "n": i})

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(one, range(N_CONCURRENT)))

    rows = actor.read("MATCH (p:Probe) WHERE p.id STARTS WITH 'w' RETURN count(p) AS n")
    assert rows[0]["n"] == N_CONCURRENT


def test_mixed_reads_and_writes(actor: GraphActor):
    def work(i: int):
        if i % 2:
            return actor.write("CREATE (:Probe {id: $id, n: 1})", {"id": f"m{i}"})
        return actor.read("MATCH (p:Probe) RETURN count(p) AS n")

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(work, range(N_CONCURRENT)))

    assert len(results) == N_CONCURRENT


def test_async_api_under_gather(actor: GraphActor):
    """This is the shape FastAPI will actually produce."""
    async def main():
        reads = [actor.aread("MATCH (p:Probe) RETURN count(p) AS n") for _ in range(N_CONCURRENT)]
        writes = [
            actor.awrite("CREATE (:Probe {id: $id, n: 1})", {"id": f"a{i}"})
            for i in range(10)
        ]
        return await asyncio.gather(*reads, *writes)

    results = asyncio.run(main())
    assert len(results) == N_CONCURRENT + 10


def test_owner_assertion_fires_on_smuggled_connection(actor: GraphActor):
    """The tripwire: reaching the pinned connection directly must be caught."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(actor._writer._execute, "MATCH (p:Probe) RETURN count(p) AS n", None)
        with pytest.raises(WrongThreadError):
            fut.result()
