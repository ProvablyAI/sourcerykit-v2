"""Tests for sourcerykit.verify: run() scoping and Run.verify on a SQLite facts table."""

import asyncio
import base64
import json
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from sourcerykit import verify as verify_module
from sourcerykit.claims import Claim, Verdict
from sourcerykit.facts import Fact
from sourcerykit.intercept import interceptor
from sourcerykit.intercept._self_egress import is_self_egress
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


ORG = uuid.uuid4()
QUERY = sqlite_facts({CALL: RESPONSE})


async def _prove(statement: str, proofs: dict[str, tuple[uuid.UUID, str]]) -> list[Any]:
    """A stand-in prover on SQLite: its "proof" of a query is the query itself."""
    proofs[statement] = (uuid.uuid4(), base64.b64encode(statement.encode()).decode())
    return await QUERY(statement)


async def _public_verifier(_client: object, _url: str, proof: str) -> dict[str, Any]:
    """The stand-in public verifier: it reruns the query its stand-in proof holds."""
    statement = base64.b64decode(proof).decode()
    rows = [[value] for value in await QUERY(statement)]
    return {"sql_query": statement, "result": {"type": "resultset", "rows": rows}, "proof": {"status": "Completed"}}


WEATHER_URL = "https://w.example"


async def _verify(
    answer: str,
    claims: list[dict[str, Any]] | str,
    *,
    also_recorded: tuple[tuple[uuid.UUID, str, str], ...] = (),
    trusted: tuple[str, ...] = (),
) -> verify_module.Report:
    """Run.verify with the recorded calls and the facts table on SQLite instead of Postgres."""

    async def check(found: list[Claim], rows: list[Fact]) -> list[verify_module.ClaimResult]:
        assert {fact.call_ref for fact in rows} == {CALL}  # only the evidence is sealed
        proofs: dict[str, tuple[uuid.UUID, str]] = {}
        return await verify_module._results(found, lambda statement: _prove(statement, proofs), proofs)

    sent: list[list[dict[str, str]]] = []

    async def complete(messages: list[dict[str, str]], model: str, *_: str) -> str:
        assert model == "test-model"
        sent.append(messages)
        return claims if isinstance(claims, str) else json.dumps({"claims": claims})

    async def is_trusted(url: str) -> bool:
        return url in trusted

    recorded = [(CALL, WEATHER_URL, RESPONSE), *also_recorded]
    run = verify_module.Run()
    run.calls.extend(call_id for call_id, _, _ in recorded)
    with (
        patch.object(verify_module, "_load_calls", AsyncMock(return_value=recorded)),
        patch.object(verify_module, "list_all_trusted_endpoints_detailed", AsyncMock(return_value=list(trusted))),
        patch.object(verify_module, "is_endpoint_trusted", is_trusted),
        patch.object(verify_module, "_check", check),
        patch.object(verify_module, "_complete", complete),
        patch.object(verify_module, "get_settings", return_value=MagicMock(org_id=ORG)),
    ):
        report = await run.verify(answer, model="test-model")
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
    assert report.claims[1].steps["v"].answer == [68]  # the recorded value, shown with the verdict
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
def test_outcome(verdicts: list[Verdict], outcome: Outcome) -> None:
    assert verify_module._outcome(verdicts) == outcome


async def test_claims_check_proven_answers_as_the_prover_returns_them() -> None:
    """The prover returns every cell as text: "6.2000" for 6.2, "<null>" for NULL, an aggregate alone."""
    two_rows = {"type": "resultset", "value": {"columns": [{"name": "num"}], "rows": [["6.2000"], ["<null>"]]}}
    assert verify_module._cells(two_rows) == ["6.2000", None]

    async def prover(statement: str) -> list[Any]:
        if "COUNT" in statement:
            return verify_module._cells({"type": "aggregate", "value": "3"})
        return verify_module._cells({"type": "resultset", "value": {"rows": [["6.2000"]]}})

    step = {"call": str(CALL), "where": {"path": "$.temperature"}}
    claim = Claim.model_validate(
        {
            "text": "6.2°C, 3 values",
            "steps": {"t": {**step, "select": "num"}, "n": {**step, "select": "count"}},
            "checks": [{"expr": "t", "op": "approx", "value": 6.2, "tol": 0.05}, {"expr": "n", "op": "=", "value": 3}],
        }
    )
    [result] = await verify_module._results([claim], prover, {})
    assert result.verdict == "supported"
    assert result.steps["t"].answer == [Decimal("6.2")] and result.steps["n"].answer == [3]


def test_a_downloaded_proof_is_kept_as_base64_of_its_bytes() -> None:
    assert base64.b64decode(verify_module._encode(b"1,2,255")) == bytes([1, 2, 255])


def _tamper_answer(report: dict[str, Any]) -> None:
    report["claims"][0]["steps"]["v"]["answer"] = ["15"]


def _tamper_verdict(report: dict[str, Any]) -> None:
    report["claims"][1]["verdict"] = "supported"
    report["outcome"] = "PASS"


def _tamper_check(report: dict[str, Any]) -> None:
    report["claims"][1]["claim"]["checks"][0]["value"] = 68
    report["claims"][1]["verdict"] = "supported"
    report["outcome"] = "PASS"


def _tamper_proof(report: dict[str, Any]) -> None:
    report["claims"][0]["steps"]["v"]["proof"] = base64.b64encode(b"SELECT 15").decode()


@pytest.mark.parametrize(
    ("tamper", "problem"),
    [
        (lambda _: None, None),
        (_tamper_answer, "proven 14.0, the report says 15"),
        (_tamper_verdict, "the proven answers give contradicted, the report says supported"),
        (_tamper_check, "not a claim of the model's reply"),
        (_tamper_proof, "the proof is for another query"),
    ],
)
async def test_check_finds_what_was_changed_in_a_report(
    tamper: Callable[[dict[str, Any]], None], problem: str | None
) -> None:
    report = await _verify(
        "It is 14°C, humidity 80%.", [_claim("14°C", "$.temperature", 14), _claim("humidity 80%", "$.humidity", 80)]
    )
    received = json.loads(report.model_dump_json())  # what a third party holds
    tamper(received)
    with patch.object(verify_module, "_verify_proof", _public_verifier):
        problems = await verify_module.check(verify_module.Report.model_validate(received))
    assert problems == [] if problem is None else any(problem in p for p in problems), problems


async def test_a_text_filter_spelled_as_the_data_does_passes_check() -> None:
    """The reply filters on "rome", the data and the proof on "Rome": check must accept it."""
    step = {"call": "call_1", "select": "count", "where": {"path": "$.city", "text": "rome"}}
    claim = {"text": "rome", "steps": {"n": step}, "checks": [{"expr": "n", "op": "=", "value": 1}]}
    report = await _verify("The city is rome.", [claim])
    assert report.outcome == Outcome.PASS and report.claims[0].claim.steps["n"].where["text"] == "Rome"
    with patch.object(verify_module, "_verify_proof", _public_verifier):
        assert await verify_module.check(report) == []


async def test_check_rejects_a_proof_the_public_verifier_refuses() -> None:
    report = await _verify("It is 14°C.", [_claim("14°C", "$.temperature", 14)])
    with patch.object(verify_module, "_verify_proof", AsyncMock(return_value=None)):
        assert await verify_module.check(report) == ["'14°C' step v: the proof does not verify"]


async def test_report_prints_the_outcome_and_each_claim_with_its_proven_values() -> None:
    report = await _verify("It is 14°C.", [_claim("14°C", "$.temperature", 14)])
    assert str(report) == "Outcome: PASS   (claims by linker:test-model)\n  SUPPORTED      '14°C'  proven: v=14.0"


EVIL = (uuid.uuid4(), "https://evil.example/weather", json.dumps({"temperature": 30}))


async def test_with_no_trusted_endpoints_every_recorded_call_is_evidence() -> None:
    report = await _verify("It is 14°C.", [_claim("14°C", "$.temperature", 14)])
    assert report.calls == [CALL] and report.untrusted == []


async def test_once_endpoints_are_trusted_a_call_to_another_is_left_out() -> None:
    claim_on_evil = {**_claim("30°C", "$.temperature", 30), "steps": {"v": {"call": "call_2", "select": "num"}}}
    report = await _verify(
        "It is 14°C, or 30°C.",
        [_claim("14°C", "$.temperature", 14), claim_on_evil],
        also_recorded=(EVIL,),
        trusted=(WEATHER_URL,),
    )
    assert report.calls == [CALL] and report.untrusted == [EVIL[1]]
    assert [r.claim.text for r in report.claims] == ["14°C"]  # the linker was never shown call_2
    assert "UNTRUSTED      https://evil.example/weather: recorded, not used as evidence" in str(report)


async def test_the_linker_is_asked_through_an_openai_compatible_chat_api_and_not_recorded() -> None:
    messages = [{"role": "user", "content": "hi"}]
    reply = '{"claims": []}'

    def chat(request: httpx.Request) -> httpx.Response:
        assert is_self_egress()  # so the interceptor does not record it
        assert str(request.url) == "https://llm.example/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer key"
        assert json.loads(request.content) == {"model": "m", "messages": messages}  # no sampling settings
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    real = httpx.AsyncClient
    with patch("sourcerykit.verify.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(chat), **kw)):
        assert await verify_module._complete(messages, "m", "https://llm.example/v1/", "key") == reply


async def test_claude_is_asked_through_its_messages_api() -> None:
    """Anthropic's URL routes to the official SDK: system prompt hoisted, only text blocks read."""
    sent: dict[str, Any] = {}

    class Messages:
        async def create(self, **request: Any) -> Any:
            assert is_self_egress()
            sent.update(request)
            blocks = [MagicMock(type="thinking"), MagicMock(type="text", text='{"claims": ')]
            return MagicMock(content=[*blocks, MagicMock(type="text", text="[]}")])

    class Claude:
        def __init__(self, *, api_key: str | None) -> None:
            assert api_key == "key"
            self.messages = Messages()

        async def __aenter__(self) -> "Claude":
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "hi"}]
    with patch("anthropic.AsyncAnthropic", Claude):
        reply = await verify_module._complete(messages, "claude-haiku-5-5", "https://api.anthropic.com", "key")
    assert reply == '{"claims": []}'
    assert sent == {
        "model": "claude-haiku-5-5",
        "max_tokens": 16000,
        "system": "rules",
        "messages": [{"role": "user", "content": "hi"}],
    }
