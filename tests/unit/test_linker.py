"""Tests for sourcerykit.linker."""

import json
import uuid
from typing import Any

from sourcerykit import linker
from sourcerykit.claims import verify
from tests.unit.test_claims import sqlite_facts

REF = uuid.uuid4()


def test_prompt_example_claims_check_out_on_its_response() -> None:
    """The worked example in the prompt must itself be valid and give the verdicts it teaches."""
    reply = json.dumps({"claims": linker._EXAMPLE_CLAIMS})
    claims, errors = linker.parse(linker._EXAMPLE_ANSWER, reply, [REF])
    assert errors == []
    query = sqlite_facts({REF: linker._EXAMPLE_CALL[1]})
    assert [verify(claim, query)[0] for claim in claims] == [
        "supported",
        "supported",
        "supported",
        "supported",
        "contradicted",  # "about €90": the price is 85
        "unsupported",
    ]


def test_messages_end_with_the_answer_and_responses() -> None:
    last = linker.messages("It is 14°C.", [("https://x.example", '{"t": 14}')])[-1]
    assert last["role"] == "user"
    assert "call: call_1" in last["content"] and '{"t": 14}' in last["content"] and "It is 14°C." in last["content"]


def test_parse_drops_bad_claims_and_keeps_the_rest() -> None:
    good = {
        "text": "14°C",
        "steps": {"t": {"call": "call_1", "select": "text", "where": {"path": "$.t"}}},
        "checks": [{"expr": "t", "op": "approx", "value": 14, "tol": 0.5}],
    }
    reply = (
        "```json\n"
        + json.dumps(
            {
                "claims": [
                    good,
                    {**good, "text": "15°C"},  # not in the answer
                    {**good, "steps": {"t": {"call": "call_9", "select": "text"}}},  # unknown call
                    {"text": "14°C", "checks": []},  # no checks
                ]
            }
        )
        + "\n```"
    )
    claims, errors = linker.parse("It is 14°C.", reply, [REF])
    assert [c.text for c in claims] == ["14°C"] and claims[0].steps["t"].call == [REF]
    assert len(errors) == 3


def test_parse_takes_the_last_claims_object() -> None:
    first = {"text": "14°C", "unsupported": "draft"}
    final = {"text": "14°C", "unsupported": "final"}
    reply = json.dumps({"claims": [first]}) + "\n\nCorrection, the final list:\n" + json.dumps({"claims": [final]})
    claims, errors = linker.parse("It is 14°C.", reply, [])
    assert errors == [] and [c.unsupported for c in claims] == ["final"]


def test_parse_drops_claims_on_paths_not_in_the_data() -> None:
    step = {"call": "call_1", "select": "min", "where": {"pattern": "$.hourly.temp[*]"}}
    claim = {"text": "14°C", "steps": {"m": step}, "checks": [{"expr": "m", "op": ">=", "value": 14}]}
    claims, errors = linker.parse("It is 14°C.", json.dumps({"claims": [claim]}), [REF], {"$.hourly.temperature_2m[*]"})
    assert claims == [] and "not in the data: ['$.hourly.temp[*]']" in errors[0]


def test_parse_rejects_a_reply_that_is_not_claims() -> None:
    assert linker.parse("x", "Sorry, I cannot help.", []) == (
        [],
        ["reply is not {'claims': [...]}: 'Sorry, I cannot help.'"],
    )


def test_parse_matches_curly_quotes_and_keeps_the_answer_s() -> None:
    claim = {"text": "isn't available", "unsupported": "no precipitation data"}
    claims, errors = linker.parse("Rain data isn\u2019t available.", json.dumps({"claims": [claim]}), [])
    assert errors == [] and claims[0].text == "isn\u2019t available"


def _price_claim(text: str, check: dict[str, Any]) -> dict[str, Any]:
    return {
        "text": text,
        "steps": {"p": {"call": "call_1", "select": "num", "where": {"path": "$.price"}}},
        "checks": [check],
    }


def test_parse_drops_claims_that_could_pass_a_value_the_quote_does_not_state() -> None:
    """The model writes the quote and the checks: these are the ways it could make "€99" pass on
    a recorded 140 without any check failing."""
    answer = "It costs €99."
    bad = [
        _price_claim("costs €99", {"expr": "p", "op": "approx", "value": 140, "tol": 0.5}),  # changed value
        _price_claim("costs €99", {"expr": "p", "op": "approx", "value": 99, "tol": 50}),  # loose tolerance
        _price_claim("costs €99", {"expr": "p - 41", "op": "approx", "value": 99, "tol": 0.5}),  # shifted value
        {"text": "costs €99", "checks": [{"expr": "99", "op": "=", "value": 99}]},  # reads no data
    ]
    claims, errors = linker.parse(answer, json.dumps({"claims": bad}), [REF])
    assert claims == [] and len(errors) == len(bad)
    assert "is in the quote" in errors[0] and "wider than half a unit" in errors[1]
    assert "only scale a unit" in errors[2] and "reads no step" in errors[3]


def test_parse_keeps_stated_values_found_in_checks_filters_and_timestamps() -> None:
    good = [
        _price_claim("costs €99", {"expr": "p", "op": "approx", "value": 99, "tol": 0.5}),
        _price_claim("about 6.3", {"expr": "p", "op": "approx", "value": 6.3, "tol": 0.05}),
        _price_claim("68%", {"expr": "p * 100", "op": "approx", "value": 68, "tol": 0.5}),  # unit scaling
        _price_claim("between 12 and 14", {"expr": "p", "op": "between", "value": [12, 14]}),
        {  # 4 is a filter constant; 18:00 sits inside the timestamp it filters on
            "text": "the 4-star hotel at 18:00",
            "steps": {
                "h": {"call": "call_1", "select": "obj", "where": {"pattern": "$.h[*].stars", "num": 4}},
                "t": {
                    "call": "call_1",
                    "select": "text",
                    "where": {"text": "2026-11-16T18:00", "obj": {"in": {"step": "h"}}},
                },
            },
            "checks": [{"expr": "t", "op": "=", "value": "2026-11-16T18:00"}],
        },
        {  # times are compared as times: 9:40 is 09:40
            "text": "lands at 9:40",
            "steps": {"t": {"call": "call_1", "select": "text", "where": {"path": "$.arrival"}}},
            "checks": [{"expr": "t", "op": "=", "value": "09:40"}],
        },
    ]
    answer = "It costs €99, about 6.3, 68%, between 12 and 14, the 4-star hotel at 18:00, lands at 9:40."
    claims, errors = linker.parse(answer, json.dumps({"claims": good}), [REF])
    assert errors == [] and len(claims) == len(good)


def test_unchecked_reports_numbers_no_claim_quotes() -> None:
    answer = "Results:\n1. BA117 costs €99 and lands at 11:40.\n2. Rain: 2.5h, on 2026-11-20."
    claim = _price_claim("costs €99", {"expr": "p", "op": "approx", "value": 99, "tol": 0.5})
    claims, _ = linker.parse(answer, json.dumps({"claims": [claim]}), [REF])
    # BA117 and 2.5h are not numbers of their own; list numbering states nothing.
    assert [text for _, text in linker.unchecked(answer, claims)] == ["11:40", "2026-11-20"]
