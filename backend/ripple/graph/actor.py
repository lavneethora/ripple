"""The single point in RIPPLE where a Relate connection is touched.

Relate connections are thread-affine: the connection belongs to the thread that
opened it, and any call from another thread raises ProgrammingError with
kind 'WrongThread'. FastAPI hands requests to arbitrary worker threads, so a
connection shared across handlers fails intermittently. Intermittent means it
survives casual testing and surfaces during a demo.

GraphActor removes the hazard structurally. The connection is opened on a
single-worker executor and every statement is dispatched back to that same
worker, so the affinity invariant holds by construction rather than by
discipline. The owner-thread assertion is a tripwire for the case where a
future refactor smuggles the raw connection out.

Nothing outside this module should import `relate` or hold a Connection.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Sequence

import relate

Row = dict[str, Any]


class WrongThreadError(RuntimeError):
    """A Relate connection was reached from a thread that does not own it."""


class _PinnedConnection:
    """One Relate connection pinned to one dedicated worker thread."""

    def __init__(self, database: str, *, read_only: bool, name: str, generation: int = 0) -> None:
        self._database = database
        self._read_only = read_only
        self.generation = generation
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self._owner_thread_id = self._executor.submit(threading.get_ident).result()
        self._conn = self._executor.submit(self._open).result()
        self._closed = False

    def _open(self) -> relate.Connection:
        if self._read_only:
            return relate.connect(self._database, read_only=True)
        return relate.connect(self._database)

    def _assert_owner(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise WrongThreadError(
                f"Relate connection for {self._database!r} was reached from thread "
                f"{threading.get_ident()} but is owned by {self._owner_thread_id}. "
                "Every statement must go through GraphActor."
            )

    def _execute(self, cypher: str, params: Row | None) -> list[Row]:
        self._assert_owner()
        cursor = self._conn.execute(cypher, params) if params else self._conn.execute(cypher)
        # Record is dict-like but iterates as KEYS, and integer indexing raises
        # KeyError. dict(record) is the only correct way to materialize a row.
        return [dict(record) for record in cursor.fetchall()]

    def _execute_many(self, statements: Sequence[tuple[str, Row | None]]) -> list[list[Row]]:
        self._assert_owner()
        out: list[list[Row]] = []
        for cypher, params in statements:
            cursor = self._conn.execute(cypher, params) if params else self._conn.execute(cypher)
            out.append([dict(record) for record in cursor.fetchall()])
        return out

    def submit(self, fn, *args):
        return self._executor.submit(fn, *args)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # close() must also run on the owning thread, or it raises WrongThread
        # and leaves the connection closed for every thread.
        self._executor.submit(self._conn.close).result()
        self._executor.shutdown(wait=True)


class GraphActor:
    """Serialized access to one Relate directory store.

    One writer connection, plus a pool of read-only connections each pinned to
    its own worker. Reads never block on the writer, which matters because the
    TMS pulls a large projection on every propagation.

    A directory store is required for this to work at all: it permits one
    writer alongside read_only readers, where a single-file .rl store refuses
    readers while a writer holds it.

    Snapshot staleness
    ------------------
    A read-only connection sees the store exactly as it was when it opened, so
    it is a frozen snapshot: writes committed afterwards are invisible to it
    forever. Measured on this engine, a reader opened before a write still
    reported the pre-write count while the writer saw the new row.

    That is a correctness hazard, not a performance one, so the actor tracks a
    write generation and transparently reopens the pool when a read arrives
    against a stale snapshot. Reopening costs roughly 100ms per 20k nodes, and
    RIPPLE writes in batches (seeding, then status writeback at the end of a
    propagation epoch) and reads many times in between, so this amortizes to
    about one refresh per epoch.
    """

    def __init__(self, database: str | Path, *, readers: int = 2) -> None:
        self._database = str(database)
        self._writer = _PinnedConnection(self._database, read_only=False, name="relate-w")
        self._readers: list[_PinnedConnection] = []
        self._reader_cursor = 0
        self._reader_lock = threading.RLock()
        self._n_readers = readers
        self._generation = 0

    def open_readers(self) -> None:
        """Open the read-only pool. Call after the store has been seeded.

        Read-only connections see the store as it was when they opened and a
        missing store is an error rather than being created, so this cannot run
        before the writer has committed something.
        """
        with self._reader_lock:
            self._open_readers_locked()

    def _open_readers_locked(self) -> None:
        for i in range(self._n_readers):
            self._readers.append(
                _PinnedConnection(
                    self._database,
                    read_only=True,
                    name=f"relate-r{i}",
                    generation=self._generation,
                )
            )

    def refresh_readers(self) -> None:
        """Reopen the read-only pool so it sees everything committed so far."""
        with self._reader_lock:
            stale = self._readers
            self._readers = []
            self._open_readers_locked()
        for conn in stale:
            conn.close()

    def _next_reader(self) -> _PinnedConnection:
        if not self._readers:
            return self._writer
        with self._reader_lock:
            # A reader opened before the latest write is a frozen snapshot and
            # would silently serve stale rows, so refresh the pool first.
            if self._readers[0].generation != self._generation:
                stale = self._readers
                self._readers = []
                self._open_readers_locked()
                for conn in stale:
                    conn.close()
            conn = self._readers[self._reader_cursor % len(self._readers)]
            self._reader_cursor += 1
        return conn

    # --- synchronous API, for scripts and tests ---

    def write(self, cypher: str, params: Row | None = None) -> list[Row]:
        rows = self._writer.submit(self._writer._execute, cypher, params).result()
        with self._reader_lock:
            self._generation += 1
        return rows

    def write_many(self, statements: Sequence[tuple[str, Row | None]]) -> list[list[Row]]:
        rows = self._writer.submit(self._writer._execute_many, statements).result()
        with self._reader_lock:
            self._generation += 1
        return rows

    def read(self, cypher: str, params: Row | None = None) -> list[Row]:
        conn = self._next_reader()
        return conn.submit(conn._execute, cypher, params).result()

    # --- async API, for FastAPI handlers ---

    async def awrite(self, cypher: str, params: Row | None = None) -> list[Row]:
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(
            self._writer._executor, self._writer._execute, cypher, params
        )
        with self._reader_lock:
            self._generation += 1
        return rows

    async def aread(self, cypher: str, params: Row | None = None) -> list[Row]:
        conn = self._next_reader()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(conn._executor, conn._execute, cypher, params)

    def close(self) -> None:
        for reader in self._readers:
            reader.close()
        self._readers.clear()
        self._writer.close()

    def __enter__(self) -> "GraphActor":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
