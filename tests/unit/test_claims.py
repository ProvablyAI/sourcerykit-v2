"""Tests for sourcerykit.claims, running the compiled SQL on an in-memory SQLite facts table."""

import asyncio
import decimal
import json
import sqlite3
import uuid
from contextlib import closing
from typing import Any

import pytest
from pydantic import ValidationError

from sourcerykit.claims import Claim, Query, Step, sql, verify
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


def sqlite_facts(responses: dict[uuid.UUID, Any]) -> Query:
    """A query function on an in-memory SQLite facts table holding ``responses``."""
    rows = []
    for call_ref, response in responses.items():
        facts = flatten(call_ref, response if isinstance(response, str) else json.dumps(response))
        assert facts is not None
        rows += [
            (str(f.call_ref), f.path, f.pattern, f.idx, f.obj, f.type, None if f.num is None else float(f.num), f.text)
            for f in facts
        ]

    async def query(statement: str) -> list[Any]:
        with closing(sqlite3.connect(":memory:")) as db:  # a database per query: nothing left open
            db.execute("CREATE TABLE facts (call_ref, path, pattern, idx INTEGER, obj, type, num REAL, text)")
            db.executemany("INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            return [row[0] for row in db.execute(statement)]

    return query


@pytest.fixture(scope="module")
def query() -> Query:
    return sqlite_facts(RESPONSES)


def _verdict(query: Any, claim: dict[str, Any]) -> str:
    return asyncio.run(verify(Claim.model_validate({"text": "-", **claim}), query))[0]


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
        verdict, queries = asyncio.run(
            verify(Claim.model_validate({"text": "The cheapest BA flight is 140", **claim}), query)
        )
        assert verdict == "supported"
        assert queries["low"] == (
            f"SELECT MIN(num) FROM facts WHERE call_ref = '{FLIGHTS}' AND pattern = '$.flights[*].price'"
            " AND obj IN ('$.flights[1]', '$.flights[2]') AND type <> 'array'",
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

    def test_aggregates_skip_array_rows(self, query: Any) -> None:
        """A pattern missing its [*] matches the array's own row, whose num is its length (4)."""
        low = _step(FLIGHTS, "min", {"pattern": "$.flights"})
        count = _step(FLIGHTS, "count", {"pattern": "$.flights"})
        claim = {"steps": {"low": low, "n": count}, "checks": [{"expr": "n", "op": "=", "value": 0}]}
        assert _verdict(query, claim) == "supported"
        assert _verdict(query, {**claim, "checks": [{"expr": "low", "op": "<", "value": 13}]}) == "contradicted"

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
        verdict, queries = asyncio.run(verify(claim, query))
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

    def test_step_named_like_a_python_keyword(self, query: Any) -> None:
        steps = {"from": _step(WEATHER, "text", {"path": "$.current.temperature_2m"})}
        assert _verdict(query, {"steps": steps, "checks": [{"expr": "from", "op": "=", "value": 14.1}]}) == "supported"

    def test_count_over_several_calls(self, query: Any) -> None:
        step = {
            **_step(TIMEOUT_1, "count", {"path": "$.error", "text": "upstream timeout"}),
            "call": [str(TIMEOUT_1), str(TIMEOUT_2)],
        }
        assert _verdict(query, {"steps": {"n": step}, "checks": [{"expr": "n", "op": "=", "value": 2}]}) == "supported"


class TestClaimShape:
    def test_unsupported_claim_runs_no_query(self) -> None:
        claim = Claim.model_validate({"text": "at £140", "unsupported": "the response has no currency"})
        assert asyncio.run(verify(claim, lambda _: pytest.fail("no query expected"))) == ("unsupported", {})

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


def test_a_step_reused_in_in_gives_the_same_sql_whatever_the_row_order() -> None:
    step = Step.model_validate({"call": WEATHER, "select": "min", "where": {"obj": {"in": {"step": "o"}}}})
    one = sql(step, {"o": ["$.f[2]", "$.f[0]", "$.f[1]"]})
    other = sql(step, {"o": ["$.f[1]", "$.f[2]", "$.f[0]", "$.f[1]"]})
    assert one == other


def test_check_arithmetic_ignores_the_host_decimal_context() -> None:
    claim = Claim.model_validate(
        {
            "text": "a third is 0.3333",
            "steps": {"one": {"call": WEATHER, "select": "num", "where": {"path": "$.one"}}},
            "checks": [{"expr": "one / 3", "op": "approx", "value": 0.3333, "tol": 0.00005}],
        }
    )
    query = sqlite_facts({WEATHER: {"one": 1}})
    with decimal.localcontext(decimal.Context(prec=2)):  # a host that lowered the precision
        assert asyncio.run(verify(claim, query))[0] == "supported"


@pytest.mark.parametrize(
    ("stated", "verdict"),
    [
        ("2026-11-23 11:00", "supported"),  # the same moment, written with a space
        ("2026-11-23T11:00:00", "supported"),
        ("2026-11-23 12:00", "contradicted"),
        ("11:00", "contradicted"),  # a time is not a date and time
        ("2026-11-23 11:00 UTC", "supported"),  # the zone written as a word
        ("2026-11-23 11:00 GMT", "supported"),
        ("2026-11-23T11:00:00+00:00", "supported"),
        ("2026-11-23T12:00:00+01:00", "supported"),  # the same instant in another zone
        ("2026-11-23 11:00 +01:00", "contradicted"),  # an hour earlier
    ],
)
def test_dates_and_times_compare_as_moments(stated: str, verdict: str) -> None:
    claim = Claim.model_validate(
        {
            "text": stated,
            "steps": {"t": {"call": WEATHER, "select": "text", "where": {"path": "$.time"}}},
            "checks": [{"expr": "t", "op": "=", "value": stated}],
        }
    )
    assert asyncio.run(verify(claim, sqlite_facts({WEATHER: {"time": "2026-11-23T11:00Z"}})))[0] == verdict


@pytest.mark.parametrize(
    ("recorded", "stated", "verdict"),
    [
        ("2026-11-23T11:00", "2026-11-23 11:00 UTC", "supported"),  # only the answer gives a zone
        ("2026-11-23T11:00", "2026-11-23T11:00", "supported"),
        ("2026-11-23T11:00", "2026-11-23 12:00 UTC", "contradicted"),
    ],
)
def test_a_time_without_a_zone_compares_by_the_clock(recorded: str, stated: str, verdict: str) -> None:
    claim = Claim.model_validate(
        {
            "text": stated,
            "steps": {"t": {"call": WEATHER, "select": "text", "where": {"path": "$.time"}}},
            "checks": [{"expr": "t", "op": "=", "value": stated}],
        }
    )
    assert asyncio.run(verify(claim, sqlite_facts({WEATHER: {"time": recorded}})))[0] == verdict


@pytest.mark.parametrize(
    ("recorded", "stated", "verdict"),
    [
        ("SHIPPED", "Shipped", "supported"),
        ("SHIPPED", "shipped", "supported"),
        ("SHIPPED", "Delivered", "contradicted"),
        ("ON_TIME", "On time", "supported"),
        ("ON_TIME", "Delayed", "contradicted"),
        ("LHR→CDG", "LHR → CDG", "supported"),
        ("LHR→CDG", "CDG → LHR", "contradicted"),
    ],
)
def test_text_compares_ignoring_case_whitespace_and_underscores(recorded: str, stated: str, verdict: str) -> None:
    claim = Claim.model_validate(
        {
            "text": f"Status: {stated}",
            "steps": {"s": {"call": WEATHER, "select": "text", "where": {"path": "$.status"}}},
            "checks": [{"expr": "s", "op": "=", "value": stated}],
        }
    )
    assert asyncio.run(verify(claim, sqlite_facts({WEATHER: {"status": recorded}})))[0] == verdict
