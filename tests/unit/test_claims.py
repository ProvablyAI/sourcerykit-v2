"""Tests for sourcerykit.claims, running the compiled SQL on an in-memory SQLite facts table."""

import json
import sqlite3
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from sourcerykit.claims import Claim, Step, sql, verify
from sourcerykit.facts import flatten

FLIGHTS = uuid.uuid4()
WEATHER = uuid.uuid4()
YESTERDAY = uuid.uuid4()
TIMEOUT_1 = uuid.uuid4()
TIMEOUT_2 = uuid.uuid4()

RESPONSES = {
    FLIGHTS: {
        "flights": [
            {"number": "AZ205", "airline": "ITA", "departure": "07:15", "price": 95},
            {"number": "BA548", "airline": "BA", "departure": "08:20", "price": 180},
            {"number": "BA117", "airline": "BA", "departure": "09:40", "price": 140},
            {"number": "FR214", "airline": "Ryanair", "departure": "12:05", "price": 42},
        ],
        "airport": "O'Hare",
        "seats": 123456789012345678901234567,
        "delayed": False,
    },
    WEATHER: {
        "current": {"temperature_2m": 14.1},
        "hourly": {
            "time": ["2026-11-16T17:00", "2026-11-16T18:00", "2026-11-16T19:00", "2026-11-16T20:00"],
            "temperature_2m": [6.0, 5.9, 6.4, 6.7],
        },
    },
    YESTERDAY: {"current": {"temperature_2m": 11.2}},
    TIMEOUT_1: {"error": "upstream timeout"},
    TIMEOUT_2: {"error": "upstream timeout"},
}


@pytest.fixture(scope="module")
def query() -> Any:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE facts (call_ref, path, pattern, idx INTEGER, obj, type, num REAL, text)")
    for call_ref, response in RESPONSES.items():
        rows = flatten(call_ref, json.dumps(response))
        assert rows is not None
        db.executemany(
            "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    str(r.call_ref),
                    r.path,
                    r.pattern,
                    r.idx,
                    r.obj,
                    r.type,
                    None if r.num is None else float(r.num),
                    r.text,
                )
                for r in rows
            ],
        )
    return lambda statement: [row[0] for row in db.execute(statement)]


def _verdict(query: Any, claim: dict[str, Any]) -> str:
    return verify(Claim.model_validate({"text": "-", **claim}), query)[0]


def _step(call: uuid.UUID, select: str, where: dict[str, Any]) -> dict[str, Any]:
    return {"call": str(call), "select": select, "where": where}


def _read(call: uuid.UUID, path: str, op: str, value: Any, **check: Any) -> dict[str, Any]:
    step = _step(call, "text", {"path": path})
    return {"steps": {"v": step}, "checks": [{"expr": "v", "op": op, "value": value, **check}]}


BA = _step(FLIGHTS, "obj", {"pattern": "$.flights[*].airline", "text": "BA"})


class TestReads:
    @pytest.mark.parametrize(
        ("path", "op", "value", "verdict"),
        [
            ("$.flights[2].departure", "=", "09:40", "supported"),
            ("$.flights[2].price", "=", 140, "supported"),
            ("$.flights[2].price", "=", 99, "contradicted"),
            ("$.flights[2].price", "=", "140.0", "contradicted"),  # text is compared as text
            ("$.seats", "=", 123456789012345678901234567, "supported"),  # beyond qedb's num range
            ("$.delayed", "=", False, "supported"),
            ("$.airport", "=", "O'Hare", "supported"),
            ("$.flights", "=", 4, "supported"),  # array length
            ("$.flights[9].price", "=", 140, "contradicted"),  # no such path
            ("$.flights[2].number", ">", 3, "contradicted"),  # not a number
        ],
    )
    def test_read(self, query: Any, path: str, op: str, value: Any, verdict: str) -> None:
        assert _verdict(query, _read(FLIGHTS, path, op, value)) == verdict

    def test_rounded_value_within_tolerance(self, query: Any) -> None:
        assert _verdict(query, _read(WEATHER, "$.current.temperature_2m", "approx", 14, tol="0.5")) == "supported"
        assert _verdict(query, _read(WEATHER, "$.current.temperature_2m", "approx", 15, tol="0.5")) == "contradicted"

    def test_quote_is_escaped(self) -> None:
        step = Step.model_validate(_step(FLIGHTS, "text", {"text": "O'Hare"}))
        assert sql(step, {}) == f"SELECT text FROM facts WHERE call_ref = '{FLIGHTS}' AND text = 'O''Hare'"


class TestChains:
    def test_aggregate_over_related_objects(self, query: Any) -> None:
        price = _step(FLIGHTS, "min", {"pattern": "$.flights[*].price", "obj": {"in": {"step": "ba"}}})
        claim = {"steps": {"ba": BA, "low": price}, "checks": [{"expr": "low", "op": "=", "value": 140}]}
        verdict, queries = verify(Claim.model_validate({"text": "The cheapest BA flight is 140", **claim}), query)
        assert verdict == "supported"
        assert queries["low"] == (
            f"SELECT MIN(num) FROM facts WHERE call_ref = '{FLIGHTS}' AND pattern = '$.flights[*].price'"
            " AND obj IN ('$.flights[1]', '$.flights[2]')",
            [140],
        )

    def test_argmin(self, query: Any) -> None:
        def step(select: str, pattern: str, **where: Any) -> dict[str, Any]:
            return _step(FLIGHTS, select, {"pattern": pattern, "obj": {"in": {"step": "ba"}}, **where})

        steps = {
            "ba": BA,
            "low": step("min", "$.flights[*].price"),
            "cheapest": step("obj", "$.flights[*].price", num={"step": "low"}),
            "number": _step(FLIGHTS, "text", {"pattern": "$.flights[*].number", "obj": {"in": {"step": "cheapest"}}}),
        }
        for number, verdict in (("BA117", "supported"), ("BA548", "contradicted")):
            claim = {"steps": steps, "checks": [{"expr": "number", "op": "=", "value": number}]}
            assert _verdict(query, claim) == verdict

    def test_slice_by_parallel_array_index(self, query: Any) -> None:
        hours = ["2026-11-16T18:00", "2026-11-16T19:00", "2026-11-16T20:00"]
        steps = {
            "hours": _step(WEATHER, "idx", {"pattern": "$.hourly.time[*]", "text": {"in": hours}}),
            "mean": _step(WEATHER, "avg", {"pattern": "$.hourly.temperature_2m[*]", "idx": {"in": {"step": "hours"}}}),
            "low": _step(WEATHER, "min", {"pattern": "$.hourly.temperature_2m[*]", "idx": {"in": {"step": "hours"}}}),
        }
        assert (
            _verdict(query, {"steps": steps, "checks": [{"expr": "mean", "op": "approx", "value": 6.33, "tol": 0.005}]})
            == "supported"
        )
        assert _verdict(query, {"steps": steps, "checks": [{"expr": "low", "op": ">=", "value": 6}]}) == "contradicted"
        assert (
            _verdict(query, {"steps": steps, "checks": [{"expr": "low", "op": "between", "value": [5, 7]}]})
            == "supported"
        )

    def test_empty_source_selects_nothing_without_a_query(self, query: Any) -> None:
        none = {**BA, "where": {"pattern": "$.flights[*].airline", "text": "Lufthansa"}}
        low = _step(FLIGHTS, "min", {"pattern": "$.flights[*].price", "obj": {"in": {"step": "none"}}})
        count = {**low, "select": "count"}
        claim = Claim.model_validate(
            {
                "text": "-",
                "steps": {"none": none, "low": low, "n": count},
                "checks": [{"expr": "n", "op": "=", "value": 0}],
            }
        )
        verdict, queries = verify(claim, query)
        assert verdict == "supported"
        assert queries["low"] == (None, [None])
        assert (
            _verdict(query, {"steps": {"none": none, "low": low}, "checks": [{"expr": "low", "op": "<", "value": 500}]})
            == "contradicted"
        )


class TestAcrossCalls:
    def test_difference_between_two_calls(self, query: Any) -> None:
        steps = {
            "today": _step(WEATHER, "text", {"path": "$.current.temperature_2m"}),
            "before": _step(YESTERDAY, "text", {"path": "$.current.temperature_2m"}),
        }
        for check, verdict in (
            ({"expr": "today - before", "op": "approx", "value": 3, "tol": 1}, "supported"),
            ({"expr": "today - before", "op": ">", "value": 0}, "supported"),
            ({"expr": "today - before", "op": "approx", "value": 5, "tol": 1}, "contradicted"),
            ({"expr": "today / (before - 11.2)", "op": ">", "value": 0}, "contradicted"),  # division by zero
        ):
            assert _verdict(query, {"steps": steps, "checks": [check]}) == verdict

    def test_count_over_several_calls(self, query: Any) -> None:
        step = {
            **_step(TIMEOUT_1, "count", {"path": "$.error", "text": "upstream timeout"}),
            "call": [str(TIMEOUT_1), str(TIMEOUT_2)],
        }
        assert _verdict(query, {"steps": {"n": step}, "checks": [{"expr": "n", "op": "=", "value": 2}]}) == "supported"


class TestClaimShape:
    def test_unsupported_claim_runs_no_query(self) -> None:
        claim = Claim.model_validate({"text": "at £140", "unsupported": "the response has no currency"})
        assert verify(claim, lambda _: pytest.fail("no query expected")) == ("unsupported", {})

    @pytest.mark.parametrize(
        "claim",
        [
            {"steps": {"v": _step(FLIGHTS, "text", {"secret": "x"})}},  # unknown column
            {"steps": {"v": _step(FLIGHTS, "text", {"idx": "1; DROP TABLE facts"})}},
            {"steps": {"v": _step(FLIGHTS, "text", {"text": {"like": "%"}})}},
            {"steps": {"v": _step(FLIGHTS, "text", {"obj": {"in": {"step": "later"}}})}},
            {"steps": {"v": {"call": "r1", "select": "text"}}},  # call is not a UUID
            {"checks": [{"expr": "v", "op": "approx", "value": 1}]},  # no tolerance
            {"checks": [{"expr": "__import__('os')", "op": "=", "value": 1}]},
            {"checks": [{"expr": "v", "op": "<", "value": "09:40"}]},
            {"checks": [{"expr": "missing", "op": "=", "value": 1}]},
            {"checks": []},
        ],
    )
    def test_invalid(self, claim: dict[str, Any]) -> None:
        valid = _read(FLIGHTS, "$.airport", "=", "x")
        with pytest.raises(ValidationError):
            Claim.model_validate({"text": "-", **valid, **claim})
