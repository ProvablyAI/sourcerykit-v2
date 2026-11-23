"""Tests for sourcerykit.verify: run() scoping and Run.verify on a SQLite facts table."""

import asyncio
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from sourcerykit import verify as verify_module
from sourcerykit.claims import Claim
from sourcerykit.facts import Fact
from sourcerykit.intercept import interceptor
from sourcerykit.intercept.interceptor import async_intercept_context
from sourcerykit.schemas.outcome import Outcome
from tests.unit.test_claims import sqlite_facts

CALL = uuid.uuid4()
RESPONSE = json.dumps({"city": "Rome", "temperature": 14, "humidity": 68, "unit": "%"})


async def _record_one() -> None:
    await interceptor._record("https://api.example/weather", "GET", {}, "{}")


async def test_run_collects_the_calls_recorded_inside_it_and_in_its_tasks() -> None:
    ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    with patch.object(interceptor, "add_intercept_row", AsyncMock(side_effect=ids)):
        async with async_intercept_context(agent_id="a", action_name="weather"):
            async with verify_module.run() as run:
                await _record_one()
                await asyncio.create_task(_record_one())  # a task gets a copy of the context
            await _record_one()  # after the block: not part of the run
    assert run.calls == ids[:2]


def _claim(text: str, path: str, value: Any) -> dict[str, Any]:
    return {
        "text": text,
        "steps": {"v": {"call": "call_1", "select": "num", "where": {"path": path}}},
        "checks": [{"expr": "v", "op": "approx", "value": value, "tol": 0.5}],
    }


async def _verify(answer: str, claims: list[dict[str, Any]] | str) -> verify_module.Report:
    """Run.verify with the recorded call and the facts table on SQLite instead of Postgres."""
    query = sqlite_facts({CALL: RESPONSE})

    async def run_claims(found: list[Claim], rows: list[Fact]) -> list[verify_module.ClaimResult]:
        assert {fact.call_ref for fact in rows} == {CALL}
        return verify_module._results(found, query)

    sent: list[list[dict[str, str]]] = []

    async def complete(messages: list[dict[str, str]]) -> str:
        sent.append(messages)
        return claims if isinstance(claims, str) else json.dumps({"claims": claims})

    run = verify_module.Run()
    run.calls.append(CALL)
    with (
        patch.object(verify_module, "_load_calls", AsyncMock(return_value=[(CALL, "https://w.example", RESPONSE)])),
        patch.object(verify_module, "_run_claims", run_claims),
    ):
        report = await run.verify(answer, complete=complete, model="test-model")
    assert RESPONSE in sent[0][-1]["content"] and report.answer in sent[0][-1]["content"]
    return report


async def test_verify_passes_an_answer_the_data_backs() -> None:
    report = await _verify("It is 14°C.", [_claim("14°C", "$.temperature", 14)])
    assert report.outcome == Outcome.PASS
    assert [r.verdict for r in report.claims] == ["supported"]
    assert report.extractor == "linker:test-model" and report.calls == [CALL]
    assert report.unchecked == [] and report.dropped == []


async def test_verify_catches_a_contradicted_value_and_reports_what_it_skipped() -> None:
    answer = "It is 14°C, humidity 80%, wind 12 km/h."
    report = await _verify(answer, [_claim("14°C", "$.temperature", 14), _claim("humidity 80%", "$.humidity", 80)])
    assert report.outcome == Outcome.CAUGHT
    assert [r.verdict for r in report.claims] == ["supported", "contradicted"]
    assert report.claims[1].queries["v"][1] == [68]  # the recorded value, shown with the verdict
    assert [text for _, text in report.unchecked] == ["12"]


async def test_verify_is_an_error_when_no_claim_can_be_checked() -> None:
    report = await _verify("It is 14°C.", "I cannot help with that.")
    assert report.outcome == Outcome.ERROR and report.claims == [] and report.dropped
    assert [text for _, text in report.unchecked] == ["14"]


async def test_verify_normalises_the_answer() -> None:
    decomposed = "Café: 14°C.\r\n"  # é as e + combining accent, Windows line end
    report = await _verify(decomposed, [_claim("14°C", "$.temperature", 14)])
    assert report.answer == "Café: 14°C.\n"


@pytest.mark.parametrize(
    ("verdicts", "outcome"),
    [
        (["supported"], Outcome.PASS),
        (["supported", "unsupported"], Outcome.PASS_WITH_NOTES),
        (["supported", "unsupported", "contradicted"], Outcome.CAUGHT),
        (["unsupported"], Outcome.ERROR),
        ([], Outcome.ERROR),
    ],
)
def test_outcome(verdicts: list[str], outcome: Outcome) -> None:
    results = [
        verify_module.ClaimResult(
            claim=Claim.model_validate({"text": "x", "unsupported": "no data"}),
            verdict=verdict,
            queries={},
        )
        for verdict in verdicts
    ]
    assert verify_module._outcome(results) == outcome
