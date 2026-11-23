"""Verify an agent's answer against the API responses recorded while it ran.

    async with sourcerykit.run() as run:
        result = await Runner.run(agent, question)  # tools call APIs inside async_intercept_context
    report = await run.verify(result.final_output, complete=ask_model, model="anthropic/claude-haiku-5.5")

The linker (one call to the caller's model) turns the answer into claims. The recorded responses,
flattened into the facts table, are sealed (committed to by the qedb middleware), and every query
the claims run on them is proven: the model only writes the checks, the verdicts come from proven
data, and the report carries the proofs.
"""

import asyncio
import base64
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from provably.service import service
from pydantic import BaseModel
from sqlalchemy import insert, select, text

from sourcerykit import linker
from sourcerykit.bootstrap.bootstrap import get_bootstrap
from sourcerykit.claims import Claim, Query, Verdict, verify
from sourcerykit.db._engine import get_engine
from sourcerykit.db._schema import facts, intercepts
from sourcerykit.errors import SourceryKitBootstrapError
from sourcerykit.facts import Fact, flatten
from sourcerykit.intercept.interceptor import _ctx_run_calls
from sourcerykit.schemas.outcome import Outcome

Complete = Callable[[list[dict[str, str]]], Awaitable[str]]
"""The caller's model: chat messages in, the text of its reply out."""


class StepResult(BaseModel):
    sql: str | None
    """The step's query on the facts table; None when its answer is known without running it (it
    filters on an empty answer, which selects no row)."""
    answer: list[Any]
    """The proven answer: the first column of every row."""
    query_id: UUID | None = None
    proof: str | None = None
    """The qedb proof of ``sql`` and ``answer``, base64."""


class ClaimResult(BaseModel):
    claim: Claim
    verdict: Verdict
    steps: dict[str, StepResult]


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
        results = await _check(claims, rows) if claims else []
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


async def _check(claims: list[Claim], rows: list[Fact]) -> list[ClaimResult]:
    """Seal ``rows`` in the facts table, then prove every query the claims run on it."""
    boot = get_bootstrap()
    middleware, table, collection = boot.middleware_id, boot.facts_table_id, boot.facts_collection_id
    if middleware is None or table is None or collection is None:
        raise SourceryKitBootstrapError("call bootstrap_system() before verify()")
    async with get_engine().begin() as lock:
        # The prover reads the sealed rows from the table until the last proof is made, so the
        # verifications of one database run one at a time; a table per verification if they must
        # run in parallel.
        await lock.execute(text(f"SELECT pg_advisory_xact_lock(hashtext('sourcerykit.{facts.name}'))"))
        async with get_engine().begin() as conn:
            await conn.execute(text(f"TRUNCATE {facts.name}"))
            if rows:
                await conn.execute(insert(facts), [_row(fact) for fact in rows])
        await service.start_preprocess(middleware, table)
        await service.get_preprocess_completed(middleware, table)

        proofs: dict[str, tuple[UUID, str]] = {}

        async def prove(statement: str) -> list[Any]:
            query_id = await service.run_query(middleware, collection, statement)
            record = await service.wait_for_proof_computation(query_id)
            proof_file = await service.get_query_proof(UUID(record["proof"]["id"]))
            proofs[statement] = (query_id, _encode(proof_file))
            return _cells(record["result"])

        return await _results(claims, prove, proofs)


async def _results(claims: list[Claim], query: Query, proofs: dict[str, tuple[UUID, str]]) -> list[ClaimResult]:
    """Claims run in parallel, the steps of each in order: a step may filter on an earlier answer."""
    verdicts = await asyncio.gather(*(verify(claim, query, table=facts.name) for claim in claims))
    results = []
    for claim, (verdict, queries) in zip(claims, verdicts, strict=True):
        steps = {}
        for name, (sql, answer) in queries.items():
            query_id, proof = proofs.get(sql or "", (None, None))
            steps[name] = StepResult(sql=sql, answer=answer, query_id=query_id, proof=proof)
        results.append(ClaimResult(claim=claim, verdict=verdict, steps=steps))
    return results


def _cells(result: dict[str, Any]) -> list[Any]:
    """The first column of a proven result: every row of a result set, or an aggregate's value."""
    if result["type"] == "aggregate":
        cells = [result["value"]]
    else:
        cells = [row[0] for row in result["value"]["rows"]]
    return [None if cell == "<null>" else cell for cell in cells]


def _encode(proof_file: bytes) -> str:
    """A downloaded proof, its bytes written as comma-separated numbers, as base64."""
    return base64.b64encode(bytes(int(byte) for byte in proof_file.split(b","))).decode()


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
