"""Verify an agent's answer against the API responses recorded while it ran.

    async with sourcerykit.run() as run:
        result = await Runner.run(agent, question)  # tools call APIs inside async_intercept_context
    report = await run.verify(result.final_output, model="anthropic/claude-haiku-5.5")

The linker (one call to the given model) turns the answer into claims. The recorded responses,
flattened into the facts table, are sealed (committed to by the qedb middleware), and every query
the claims run on them is proven: the model only writes the checks, the verdicts come from proven
data, and the report carries the proofs, which ``check`` lets anyone verify.
"""

import asyncio
import base64
import unicodedata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from provably.service import service
from pydantic import BaseModel
from sqlalchemy import insert, select, text

from sourcerykit import linker
from sourcerykit.bootstrap.bootstrap import get_bootstrap
from sourcerykit.claims import Claim, Query, Verdict, verify
from sourcerykit.config import DEFAULT_PROVABLY_API_URL, get_settings
from sourcerykit.db._engine import get_engine
from sourcerykit.db._schema import facts, intercepts
from sourcerykit.errors import SourceryKitBootstrapError, SourceryKitConfigError
from sourcerykit.facts import Fact, flatten
from sourcerykit.intercept._self_egress import provably_self_egress
from sourcerykit.intercept.interceptor import _ctx_run_calls
from sourcerykit.schemas.outcome import Outcome
from sourcerykit.trusted_endpoints import is_endpoint_trusted, list_all_trusted_endpoints_detailed


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
    org_id: UUID
    """The Provably organization whose middleware made the proofs."""
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
    """Intercept row ids of the recorded calls used as evidence, in order: the linker sees them as
    call_1, call_2..."""
    claims: list[ClaimResult]
    dropped: list[str]
    """Claims the model wrote that were rejected, and why."""
    unchecked: list[tuple[int, str]]
    """Offset and text of each number, time and date in the answer that no claim quotes."""
    untrusted: list[str] = []
    """URLs of the recorded calls left out as evidence: the project trusts other endpoints only."""

    def __str__(self) -> str:
        lines = [f"Outcome: {self.outcome}   (claims by {self.extractor})"]
        for result in self.claims:
            if result.claim.unsupported is not None:
                lines.append(f"  NOT VERIFIABLE {result.claim.text!r}  {result.claim.unsupported}")
                continue
            proven = "  ".join(f"{name}={_show(step.answer)}" for name, step in result.steps.items())
            lines.append(f"  {result.verdict.upper():14} {result.claim.text!r}  proven: {proven}")
        lines += [f"  DROPPED        {reason}" for reason in self.dropped]
        lines += [f"  UNCHECKED      {text!r}: no claim checks it" for _, text in self.unchecked]
        lines += [f"  UNTRUSTED      {url}: recorded, not used as evidence" for url in self.untrusted]
        return "\n".join(lines)


class Run:
    """The API calls recorded inside one ``run()`` block, and the verification of its answer."""

    def __init__(self) -> None:
        self.calls: list[UUID] = []

    async def verify(
        self, answer: str, *, model: str, base_url: str | None = None, api_key: str | None = None
    ) -> Report:
        """Check ``answer`` against the calls recorded in this run.

        ``model`` turns the answer into claims, through the OpenAI-compatible chat API at
        ``base_url`` with ``api_key``: by default ``SOURCERYKIT_LINKER_URL`` and
        ``SOURCERYKIT_LINKER_API_KEY``.
        """
        settings = get_settings()
        answer = unicodedata.normalize("NFC", answer).replace("\r\n", "\n")
        recorded, untrusted = await _by_trust(await _load_calls(self.calls))
        rows = [fact for call_id, _, raw in recorded for fact in flatten(call_id, raw) or []]
        messages = linker.messages(answer, [(url, raw) for _, url, raw in recorded])
        reply = await _complete(messages, model, base_url or settings.linker_url, api_key or settings.linker_api_key)
        paths = {p for fact in rows for p in (fact.path, fact.pattern)}
        texts = {fact.text for fact in rows if fact.text is not None}
        claims, dropped = linker.parse(answer, reply, [call_id for call_id, _, _ in recorded], paths, texts)
        results = await _check(claims, rows) if claims else []
        return Report(
            org_id=settings.org_id,
            outcome=_outcome([result.verdict for result in results]),
            answer=answer,
            extractor=f"linker:{model}",
            reply=reply,
            calls=[call_id for call_id, _, _ in recorded],
            claims=results,
            dropped=dropped,
            unchecked=linker.unchecked(answer, claims),
            untrusted=untrusted,
        )


async def check(report: Report, *, api_url: str = DEFAULT_PROVABLY_API_URL) -> list[str]:
    """What is wrong with ``report``; nothing if it holds. Needs no Provably account.

    The public verifier checks every proof and returns the query and answer it proves; each claim
    must be one the model's reply holds and the linker's guards accept; every verdict and the
    outcome are recomputed from the proven answers.
    """
    problems: list[str] = []
    proven: dict[str, list[Any]] = {}
    url = f"{api_url.rstrip('/')}/api/v1/organizations/{report.org_id}/queries/verify_proof"
    async with httpx.AsyncClient(timeout=120) as client:
        for result in report.claims:
            for name, step in result.steps.items():
                if step.sql is None:
                    continue
                if step.proof is None:
                    problems.append(f"{result.claim.text!r} step {name}: no proof")
                    continue
                verified = await _verify_proof(client, url, step.proof)
                if verified is None or verified["proof"]["status"] != "Completed":
                    problems.append(f"{result.claim.text!r} step {name}: the proof does not verify")
                elif verified["sql_query"] != step.sql:
                    problems.append(f"{result.claim.text!r} step {name}: the proof is for another query")
                else:
                    proven[step.sql] = _cells(verified["result"])

    async def query(statement: str) -> list[Any]:
        if statement not in proven:
            raise _NotProven(statement)
        return proven[statement]

    # The report's text filters stand in for the data's spellings, which its proofs bind
    spellings = {text for result in report.claims for text in linker.filter_texts(result.claim)}
    allowed, _ = linker.parse(report.answer, report.reply, report.calls, texts=spellings)
    verdicts = []
    for result in report.claims:
        text = repr(result.claim.text)
        if result.claim not in allowed:
            problems.append(f"{text}: not a claim of the model's reply that the guards accept")
        try:
            verdict, queries = await verify(result.claim, query, table=facts.name)
        except _NotProven:
            continue  # its step is already reported
        verdicts.append(verdict)
        if verdict != result.verdict:
            problems.append(f"{text}: the proven answers give {verdict}, the report says {result.verdict}")
        for name, (_, answer) in queries.items():
            stated = _show(result.steps[name].answer)
            if _show(answer) != stated:
                problems.append(f"{text} step {name}: proven {_show(answer)}, the report says {stated}")
    if not problems and _outcome(verdicts) != report.outcome:
        problems.append(f"the verdicts give {_outcome(verdicts)}, the report says {report.outcome}")
    return problems


async def _complete(messages: list[dict[str, str]], model: str, base_url: str, api_key: str) -> str:
    """The linker model's reply to ``messages``: through Claude's Messages API when ``base_url`` is
    Anthropic's, otherwise through an OpenAI-compatible chat API.

    No sampling settings are sent: current models reject them, and a model's reply is not
    reproducible anyway. The report keeps the reply, from which anyone can rebuild every claim.
    """
    if not base_url:
        raise SourceryKitConfigError("set SOURCERYKIT_LINKER_URL, or pass base_url, for the linker model")
    with provably_self_egress():  # the linker's traffic is not evidence
        if urlsplit(base_url).hostname == _ANTHROPIC_HOST:
            return await _complete_claude(messages, model, api_key)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}/chat/completions", headers=headers, json={"model": model, "messages": messages}
            )
    response.raise_for_status()
    content: str | None = response.json()["choices"][0]["message"]["content"]
    return content or ""


_ANTHROPIC_HOST = "api.anthropic.com"


async def _complete_claude(messages: list[dict[str, str]], model: str, api_key: str) -> str:
    """The reply through the official SDK; without ``api_key`` it finds its own credentials."""
    try:
        from anthropic import AsyncAnthropic  # noqa: PLC0415
    except ImportError as e:
        raise SourceryKitConfigError(
            "Claude's API needs the anthropic package: pip install 'sourcerykit[anthropic]'"
        ) from e
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns: Any = [m for m in messages if m["role"] != "system"]
    async with AsyncAnthropic(api_key=api_key or None) as client:
        response = await client.messages.create(model=model, max_tokens=16000, system=system, messages=turns)
    return "".join(block.text for block in response.content if block.type == "text")


class _NotProven(LookupError):
    """A query a claim runs has no verified proof."""


async def _verify_proof(client: httpx.AsyncClient, url: str, proof: str) -> dict[str, Any] | None:
    """The public verifier's answer for ``proof``, or None if it rejects it."""
    proof_file = ",".join(str(byte) for byte in base64.b64decode(proof))
    with provably_self_egress():
        response = await client.post(url, files={"proof_file": ("proof.provably", proof_file)})
    if response.status_code >= 400:
        return None
    answer: dict[str, Any] = response.json()
    return answer


def _show(values: list[Any]) -> str:
    return str(values[0]) if len(values) == 1 else "[" + ", ".join(map(str, values)) + "]"


@asynccontextmanager
async def run() -> AsyncIterator[Run]:
    """Collect the API calls recorded inside the block, including those of tasks it starts."""
    current = Run()
    token = _ctx_run_calls.set(current.calls)
    try:
        yield current
    finally:
        _ctx_run_calls.reset(token)


def _outcome(verdict_list: list[Verdict]) -> Outcome:
    verdicts = set(verdict_list)
    if "contradicted" in verdicts:
        return Outcome.CAUGHT
    if "supported" not in verdicts:
        return Outcome.ERROR
    return Outcome.PASS_WITH_NOTES if "unsupported" in verdicts else Outcome.PASS


async def _by_trust(recorded: list[tuple[UUID, str, str]]) -> tuple[list[tuple[UUID, str, str]], list[str]]:
    """The calls that count as evidence, and the URLs of the others.

    With no trusted endpoints, every recorded call counts. Once a project lists some, only calls to
    them do, so a tool whose URL the model can steer cannot bring in data of its own choosing.
    """
    if not recorded or not await list_all_trusted_endpoints_detailed():
        return recorded, []
    trusted = await asyncio.gather(*(is_endpoint_trusted(url) for _, url, _ in recorded))
    evidence = [call for call, ok in zip(recorded, trusted, strict=True) if ok]
    return evidence, [url for (_, url, _), ok in zip(recorded, trusted, strict=True) if not ok]


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
        table = result.get("value", result)  # the public verifier inlines the table
        cells = [row[0] for row in table["rows"]]
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
