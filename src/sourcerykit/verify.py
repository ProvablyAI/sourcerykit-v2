"""Verify an agent's answer against the API responses recorded while it ran.

    async with sourcerykit.run() as run:
        result = await Runner.run(agent, question)  # tools call APIs inside async_intercept_context
    report = await run.verify(result.final_output, complete=ask_model, model="anthropic/claude-haiku-5.5")

The linker (one call to the caller's model) turns the answer into claims; each claim's queries
run on the recorded responses, flattened into the facts table. The model only writes the
checks: the verdicts come from the data.
"""

import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Connection, insert, select, text

from sourcerykit import linker
from sourcerykit.claims import Claim, Query, Verdict, verify
from sourcerykit.db._engine import get_engine
from sourcerykit.db._schema import facts, intercepts
from sourcerykit.facts import Fact, flatten
from sourcerykit.intercept.interceptor import _ctx_run_calls
from sourcerykit.schemas.outcome import Outcome

Complete = Callable[[list[dict[str, str]]], Awaitable[str]]
"""The caller's model: chat messages in, the text of its reply out."""


class ClaimResult(BaseModel):
    claim: Claim
    verdict: Verdict
    queries: dict[str, tuple[str | None, list[Any]]]
    """Each step's SQL on the facts table, and what it returned."""


class Report(BaseModel):
    outcome: Outcome
    """CAUGHT if any claim is contradicted; else ERROR if none is supported; else PASS_WITH_NOTES if
    some have no data to check them (the unsupported ones), PASS if every claim is supported."""
    answer: str
    """The answer as verified: Unicode NFC, ``\\n`` line ends."""
    extractor: str
    """What turned the answer into claims, e.g. ``linker:anthropic/claude-haiku-5.5``."""
    reply: str
    """The model's reply, as received: anyone can re-check its claims, but not re-run the model."""
    calls: list[UUID]
    """Intercept row ids of the recorded calls, in order: the linker sees them as call_1, call_2..."""
    claims: list[ClaimResult]
    dropped: list[str]
    """Claims the model wrote that were rejected, and why."""
    unchecked: list[tuple[int, str]]
    """Offset and text of each number, time and date in the answer that no claim quotes."""


class Run:
    """The API calls recorded inside one ``run()`` block, and the verification of its answer."""

    def __init__(self) -> None:
        self.calls: list[UUID] = []

    async def verify(self, answer: str, *, complete: Complete, model: str) -> Report:
        """Check ``answer`` against the calls recorded in this run; ``model`` labels the report.

        Call it after the ``run()`` block, so the model's own HTTP traffic is not recorded.
        """
        answer = unicodedata.normalize("NFC", answer).replace("\r\n", "\n")
        recorded = await _load_calls(self.calls)
        rows = [fact for call_id, _, raw in recorded for fact in flatten(call_id, raw) or []]
        reply = await complete(linker.messages(answer, [(url, raw) for _, url, raw in recorded]))
        paths = {p for fact in rows for p in (fact.path, fact.pattern)}
        claims, dropped = linker.parse(answer, reply, [call_id for call_id, _, _ in recorded], paths)
        results = await _run_claims(claims, rows) if claims else []
        return Report(
            outcome=_outcome(results),
            answer=answer,
            extractor=f"linker:{model}",
            reply=reply,
            calls=[call_id for call_id, _, _ in recorded],
            claims=results,
            dropped=dropped,
            unchecked=linker.unchecked(answer, claims),
        )


@asynccontextmanager
async def run() -> AsyncIterator[Run]:
    """Collect the API calls recorded inside the block, including those of tasks it starts."""
    current = Run()
    token = _ctx_run_calls.set(current.calls)
    try:
        yield current
    finally:
        _ctx_run_calls.reset(token)


def _outcome(results: list[ClaimResult]) -> Outcome:
    verdicts = {result.verdict for result in results}
    if "contradicted" in verdicts:
        return Outcome.CAUGHT
    if "supported" not in verdicts:
        return Outcome.ERROR
    return Outcome.PASS_WITH_NOTES if "unsupported" in verdicts else Outcome.PASS


async def _load_calls(call_ids: list[UUID]) -> list[tuple[UUID, str, str]]:
    """(id, url, raw response) of each recorded call, in the order they were recorded."""
    if not call_ids:
        return []
    statement = select(intercepts.c.id, intercepts.c.source_url, intercepts.c.raw_response).where(
        intercepts.c.id.in_(call_ids)
    )
    async with get_engine().connect() as conn:
        found = {row.id: (row.id, row.source_url, row.raw_response) for row in await conn.execute(statement)}
    return [found[call_id] for call_id in call_ids if call_id in found]


async def _run_claims(claims: list[Claim], rows: list[Fact]) -> list[ClaimResult]:
    """Refill the facts table with ``rows`` and run every claim on it, in one transaction."""
    async with get_engine().begin() as conn:
        # TRUNCATE locks the table until commit, so verifications of one project run one
        # at a time; a table per verification if they must run in parallel.
        await conn.execute(text(f"TRUNCATE {facts.name}"))
        if rows:
            await conn.execute(insert(facts), [_row(fact) for fact in rows])
        return await conn.run_sync(lambda sync: _results(claims, _driver_query(sync)))


def _driver_query(conn: Connection) -> Query:
    def query(statement: str) -> list[Any]:
        # psycopg reads % as a placeholder whenever SQLAlchemy passes parameters, even none: the
        # SQL holds only literals, so every % is a literal one ("%" is a unit).
        return list(conn.exec_driver_sql(statement.replace("%", "%%")).scalars())

    return query


def _results(claims: list[Claim], query: Query) -> list[ClaimResult]:
    results = []
    for claim in claims:
        verdict, queries = verify(claim, query, table=facts.name)
        results.append(ClaimResult(claim=claim, verdict=verdict, queries=queries))
    return results


def _row(fact: Fact) -> dict[str, Any]:
    return {
        "call_ref": fact.call_ref,
        "path": fact.path,
        "pattern": fact.pattern,
        "idx": fact.idx,
        "obj": fact.obj,
        "type": fact.type,
        "num": fact.num,
        "text": fact.text,
    }
