"""Turns an agent's answer into claims about the API responses the agent received.

The linker is one LLM call: the answer and the recorded responses go in, and each factual
statement of the answer comes out as a claim (see ``sourcerykit.claims``) quoting the statement
and saying how to check it. The model only writes the checks; the verdict comes from running
them on the recorded data. This module builds the messages and parses the reply; ``Run.verify``
makes the call.
"""

import ast
import json
import re
from collections.abc import Iterable
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from sourcerykit.claims import _AGGREGATES, Claim, Step, folded
from sourcerykit.facts import Fact

INSTRUCTIONS = """\
You turn an AI agent's answer into claims that can be checked against the API responses the
agent received. You do not decide whether a claim is true: you write how to check it, and the
checks run on the recorded data.

## The data

Each response is flattened into rows of a table, one per JSON value and one per array:
- path: exact location, like $.hotels[2].price (keys as .key, or ['key'] if not an identifier).
- pattern: the path with every array index as [*], like $.hotels[*].price. A list of plain
  values, like {"temps": [3, 5]}, has them at $.temps[*]; $.temps is the list's own row, which
  holds only its length.
- idx: the index in the innermost array.
- obj: the path of the innermost array element, like $.hotels[2].
- type: number, string, bool, null or array.
- num: the number (booleans 1 or 0, arrays their length).
- text: the value as text: numbers without trailing zeros, booleans true or false, arrays their
  length.

## A claim

{"text": "<statement quoted from the answer>", "steps": {"<name>": <step>, ...}, "checks": [<check>, ...]}
or, when no response backs or refutes the statement:
{"text": "<statement>", "unsupported": "<what the responses lack>"}

A step is one query on one or more responses:
{"call": "<call>" or ["<call>", ...], "select": <what>, "where": {<column>: <filter>, ...}}
- select: a column (text, num, obj or idx), or an aggregate of num (count, min, max, sum, avg).
- filter: a constant (equality); or {"<op>": <constant>} with op =, !=, <, <=, >, >=, between
  ([low, high]) or in ([values]); or {"step": "<earlier step>"} for the single value it
  returned; or {"in": {"step": "<earlier step>"}} for all the values it returned.
- Text columns (path, pattern, obj, type, text) compare only with = or in.

A check compares what the steps returned with what the answer states:
{"expr": "<step name, or + - * / of step names and numbers>", "op": <op>, "value": <value>, "tol": <number>}
- op: =, !=, <, <=, >, >=, between ([low, high]), or approx (with tol).
- A string or boolean value compares with = or != against one step selecting text.
- All checks must hold for the statement to be supported.

## Rules

1. Write a claim for every factual statement about the data: values, counts, comparisons,
   extremes, units, currencies, and statements about errors. Skip opinions, advice, questions,
   offers, and what the agent says it can or cannot do.
2. Quote text verbatim from the answer: one contiguous span, the shortest that states the fact,
   without ellipsis. One fact per claim: split statements joined by "and" or commas into
   separate claims; a value and its unit or currency are one fact.
3. Take every value from the answer, never from the data, even when they differ: the checks
   decide.
4. Find things by their data, not by position. To read a field of one item, first select the obj
   whose identifying field matches, then the field where obj is in that step. For part of a time
   series, select the idx whose times are the ones meant, then aggregate where idx is in that step.
5. Numbers: use approx with tol half a unit of the last digit stated (14 -> 0.5, 6.33 -> 0.005,
   140 -> 0.5), also for approximate wording.
6. A unit or currency stated with a value is part of the claim: check it against the field
   that gives it (like current_units or currency). If no response gives it, the claim is
   unsupported.
7. If the responses hold nothing that backs or refutes a statement, it is unsupported. Never
   check something other than what the statement says.
8. Text is compared as written, ignoring only case, spacing and underscores ("On time" matches
   ON_TIME). If the answer names the data's value in other words (a name for a code, "United
   States" for US, "London to Paris" for LHR→CDG), the statement is unsupported: give the value
   the data writes. If it states a different value ("Delayed" where the data says ON_TIME),
   check it: the check decides.

Reply with JSON only: {"claims": [...]}.
"""

_EXAMPLE_CALL = (
    "https://api.hotels.example/v1/search",
    json.dumps(
        {
            "city": "Rome",
            "currency": "EUR",
            "hotels": [
                {"name": "Hotel Roma", "stars": 4, "price": 120},
                {"name": "Albergo Sole", "stars": 3, "price": 85},
                {"name": "Palazzo Blu", "stars": 4, "price": 210},
            ],
        }
    ),
)
_EXAMPLE_ANSWER = (
    "I found 3 hotels in Rome. The cheapest 4-star hotel is Hotel Roma at €120, and Albergo Sole "
    "costs about €90. All of them have free parking. Rome is lovely in spring!"
)


def _hotel(name: str, field: str) -> dict[str, Any]:
    return {
        "h": {"call": "call_1", "select": "obj", "where": {"pattern": "$.hotels[*].name", "text": name}},
        "v": {
            "call": "call_1",
            "select": "text",
            "where": {"pattern": f"$.hotels[*].{field}", "obj": {"in": {"step": "h"}}},
        },
    }


_CURRENCY = {"call": "call_1", "select": "text", "where": {"path": "$.currency"}}
_EXAMPLE_CLAIMS = [
    {
        "text": "I found 3 hotels",
        "steps": {"n": {"call": "call_1", "select": "text", "where": {"path": "$.hotels"}}},
        "checks": [{"expr": "n", "op": "approx", "value": 3, "tol": 0.5}],
    },
    {
        "text": "in Rome",
        "steps": {"city": {"call": "call_1", "select": "text", "where": {"path": "$.city"}}},
        "checks": [{"expr": "city", "op": "=", "value": "Rome"}],
    },
    {
        "text": "The cheapest 4-star hotel is Hotel Roma",
        "steps": {
            "four": {"call": "call_1", "select": "obj", "where": {"pattern": "$.hotels[*].stars", "num": 4}},
            "low": {
                "call": "call_1",
                "select": "min",
                "where": {"pattern": "$.hotels[*].price", "obj": {"in": {"step": "four"}}},
            },
            "cheapest": {
                "call": "call_1",
                "select": "obj",
                "where": {"pattern": "$.hotels[*].price", "obj": {"in": {"step": "four"}}, "num": {"step": "low"}},
            },
            "name": {
                "call": "call_1",
                "select": "text",
                "where": {"pattern": "$.hotels[*].name", "obj": {"in": {"step": "cheapest"}}},
            },
        },
        "checks": [{"expr": "name", "op": "=", "value": "Hotel Roma"}],
    },
    {
        "text": "Hotel Roma at €120",
        "steps": {**_hotel("Hotel Roma", "price"), "c": _CURRENCY},
        "checks": [{"expr": "v", "op": "approx", "value": 120, "tol": 0.5}, {"expr": "c", "op": "=", "value": "EUR"}],
    },
    {
        "text": "Albergo Sole costs about €90",
        "steps": {**_hotel("Albergo Sole", "price"), "c": _CURRENCY},
        "checks": [{"expr": "v", "op": "approx", "value": 90, "tol": 0.5}, {"expr": "c", "op": "=", "value": "EUR"}],
    },
    {"text": "All of them have free parking", "unsupported": "the response has no parking information"},
]


def messages(answer: str, calls: list[tuple[str, str]]) -> list[dict[str, str]]:
    """Chat messages asking for the claims of ``answer``; ``calls`` holds (url, response text).

    The model names the calls call_1, call_2..., in this order: short names it copies reliably.
    """
    return [
        {"role": "system", "content": INSTRUCTIONS},
        {"role": "user", "content": _request(_EXAMPLE_ANSWER, [_EXAMPLE_CALL])},
        {"role": "assistant", "content": json.dumps({"claims": _EXAMPLE_CLAIMS}, ensure_ascii=False)},
        {"role": "user", "content": _request(answer, calls)},
    ]


def parse(
    answer: str, reply: str, calls: list[UUID], facts: Iterable[Fact] | None = None, texts: Iterable[str] = ()
) -> tuple[list[Claim], list[str]]:
    """The valid claims of the model's ``reply``, and why each other one was dropped.

    ``calls`` holds the UUIDs of the recorded calls, in the order given to ``messages``, and
    ``facts`` their rows. A claim is dropped if it is malformed, does not quote the answer, reads a
    call that was not shown, or filters on a path or pattern that is not in the data. It is also
    dropped if a step filters on text, or aggregates, on a path or pattern only arrays have: an
    array's row holds just its length, so such a step selects nothing whatever the values (the
    elements of ``$.a`` are ``$.a[*]``). Either mistake would pass as a contradicted claim.

    A text filter is spelled as in ``texts``, or in the facts, when exactly one of them is the same
    text once folded (``claims.folded``): the answer's "no physical damage" filters on the data's
    "No physical damage", rather than select nothing.
    """
    rows = None if facts is None else list(facts)
    paths = None if rows is None else {p for f in rows for p in (f.path, f.pattern)}
    arrays = set() if rows is None else {p for f in rows if f.type == "array" for p in (f.path, f.pattern)}
    arrays -= {p for f in rows or [] if f.type != "array" for p in (f.path, f.pattern)}
    texts = [*texts, *(f.text for f in rows or [] if f.text is not None)]
    items = _claims(reply)
    if items is None:
        return [], [f"reply is not {{'claims': [...]}}: {reply[:200]!r}"]
    names = {f"call_{i}": str(call) for i, call in enumerate(calls, 1)}
    spellings: dict[str, set[str]] = {}
    for text in texts:
        spellings.setdefault(folded(text), set()).add(text)
    claims, errors = [], []
    for item in items:
        try:
            item["text"] = _quote(answer, item["text"])
            for step in item.get("steps", {}).values():
                refs = step["call"] if isinstance(step["call"], list) else [step["call"]]
                step["call"] = [names[ref] for ref in refs]
            claim = Claim.model_validate(item)
            unknown = set() if paths is None else _filtered_paths(claim.steps.values()) - paths
            if unknown:
                raise ValueError(f"not in the data: {sorted(unknown)}")
            on_arrays = {
                p
                for step in claim.steps.values()
                if "text" in step.where or step.select in _AGGREGATES
                for p in _filtered_paths([step]) & arrays
            }
            if on_arrays:
                raise ValueError(f"{sorted(on_arrays)} only name arrays: their elements are '<path>[*]'")
            if claim.unsupported is None:
                _guard(claim)
            _respell(claim, spellings)
            claims.append(claim)
        except (ValidationError, ValueError, KeyError, TypeError) as e:
            errors.append(f"{str(item)[:120]}: {e!r}")
    return claims, errors


def unchecked(answer: str, claims: list[Claim]) -> list[tuple[int, str]]:
    """Offset and text of each number, time and date in ``answer`` that no claim quotes.

    The linker decides what to extract, so what it skips must be visible rather than pass
    silently: a statement it left out, or one an injected instruction talked it out of. The
    numbering of a list ("1. ") states nothing and is left out.
    """
    covered = [span for claim in claims for span in _occurrences(answer, claim.text)]
    found = []
    for m in _NUMBER.finditer(answer):
        line_start = answer.rfind("\n", 0, m.start()) + 1
        if not answer[line_start : m.start()].strip() and answer[m.end() : m.end() + 2] in (". ", ") "):
            continue
        if not any(start <= m.start() and m.end() <= end for start, end in covered):
            found.append((m.start(), m.group()))
    return found


def _occurrences(text: str, part: str) -> list[tuple[int, int]]:
    spans = []
    start = text.find(part)
    while start >= 0:
        spans.append((start, start + len(part)))
        start = text.find(part, start + 1)
    return spans


# Where a number, time or date is written in English text; Decimal, date and datetime parse what
# it says (_value)
_NUMBER = re.compile(
    r"""
    (?<![\w.,:])                     # not inside a word (BA117) or a longer number
    (?: \d{4}-\d{2}-\d{2}            # date: 2026-11-20
      | \d{1,2}:\d{2}(?::\d{2})?     # time: 9:40, 18:00:30
      | \d{1,3}(?:,\d{3})+(?:\.\d+)? # thousands: 1,200.5
      | \d+(?:\.\d+)?                # plain: 140, 6.33
    )
    (?!\w|[.,:]\d)                   # nor the start of one: "2.5h" and "3rd" yield nothing
    """,
    re.VERBOSE,
)
_NOT_STATED = {"path", "pattern", "obj", "idx"}  # positions in the data, not values an answer states


def _guard(claim: Claim) -> None:
    """Reject a claim that could hold for a value other than the one its quote states.

    The model writes both the quote and the checks, so nothing but these rules stops it from
    checking 140 where the answer says €99, from a tolerance wide enough to cover both, or from
    an expression that shifts the recorded value onto the stated one.
    """
    stated = _stated(claim)
    for m in _NUMBER.finditer(claim.text):
        if _value(m.group()) not in stated:
            raise ValueError(f"{m.group()} is in the quote, but no check or filter uses it")
    for check in claim.checks:
        if check.op == "approx" and check.tol is not None:
            exponent = Decimal(str(check.value)).as_tuple().exponent
            limit = Decimal("0.5").scaleb(min(exponent, 0) if isinstance(exponent, int) else 0)
            if check.tol > limit:
                raise ValueError(f"tol {check.tol} is wider than half a unit of {check.value} ({limit})")
        if not check.expr.isidentifier():
            tree = ast.parse(check.expr, mode="eval")
            scaling = {
                id(side)
                for node in ast.walk(tree)
                if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Div))
                for side in (node.left, node.right)
            }
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and not (id(node) in scaling and _power_of_ten(node.value)):
                    raise ValueError(f"{check.expr!r}: a number in an expression may only scale a unit")


Value = Decimal | date | time


def _stated(claim: Claim) -> set[Value]:
    """The numbers, times and dates the claim's checks compare with and its steps filter on,
    including those written inside text constants: 18:00 of "2026-11-16T18:00"."""
    stated: set[Value] = set()

    def add(value: Any) -> None:
        if isinstance(value, dict):
            for v in value.values():
                add(v)
        elif isinstance(value, list):
            for v in value:
                add(v)
        elif isinstance(value, str):
            stated.update(v for m in _NUMBER.finditer(value) if (v := _value(m.group())) is not None)
            try:
                moment = datetime.fromisoformat(value)
            except ValueError:
                return
            stated.update((moment.date(), moment.time()))
        elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            stated.add(abs(Decimal(str(value))))  # the quote's "-" is not part of the match

    for check in claim.checks:
        add(check.value)
    for step in claim.steps.values():
        add({column: value for column, value in step.where.items() if column not in _NOT_STATED})
    return stated


def _value(text: str) -> Value | None:
    """What a number, time or date found by ``_NUMBER`` says, or None if it is impossible (25:00)."""
    try:
        if ":" in text:
            return datetime.strptime(text, "%H:%M:%S" if text.count(":") == 2 else "%H:%M").time()
        if "-" in text:
            return date.fromisoformat(text)
        return Decimal(text.replace(",", ""))
    except ValueError:
        return None


def _power_of_ten(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = Decimal(str(value)).normalize()
    return number > 0 and number.as_tuple().digits == (1,)


def _claims(reply: str) -> list[Any] | None:
    """The claims of the last ``{"claims": [...]}`` object in ``reply``: models sometimes send a
    corrected list after a first one, or wrap it in a code fence."""
    decoder = json.JSONDecoder()
    found = None
    start = reply.find("{")
    while start >= 0:
        try:
            value, end = decoder.raw_decode(reply, start)
        except ValueError:
            start = reply.find("{", start + 1)
            continue
        if isinstance(value, dict) and isinstance(value.get("claims"), list):
            found = value["claims"]
        start = reply.find("{", end)
    return found


def _filtered_paths(steps: Iterable[Step]) -> set[str]:
    """The constant paths and patterns the steps filter on."""
    found: set[str] = set()
    for step in steps:
        for column in ("path", "pattern"):
            value = step.where.get(column)
            if isinstance(value, dict) and len(value) == 1:
                value = next(iter(value.values()))
            found.update(v for v in (value if isinstance(value, list) else [value]) if isinstance(v, str))
    return found


def filter_texts(claim: Claim) -> set[str]:
    """The constants the claim's steps filter the ``text`` column on."""
    found: set[str] = set()
    for step in claim.steps.values():
        value = step.where.get("text")
        operands = [value] if not isinstance(value, dict) else [] if "step" in value else list(value.values())
        for operand in operands:
            found.update(v for v in (operand if isinstance(operand, list) else [operand]) if isinstance(v, str))
    return found


def _respell(claim: Claim, spellings: dict[str, set[str]]) -> None:
    """Spell the claim's text filters as the data does, where a single spelling matches."""

    def spelled(value: Any) -> Any:
        if isinstance(value, str) and len(found := spellings.get(folded(value), set())) == 1:
            return next(iter(found))
        return value

    for step in claim.steps.values():
        value = step.where.get("text")
        if isinstance(value, str):
            step.where["text"] = spelled(value)
        elif isinstance(value, dict) and "step" not in value:
            step.where["text"] = {
                op: [spelled(v) for v in operand] if isinstance(operand, list) else spelled(operand)
                for op, operand in value.items()
            }


_QUOTES = str.maketrans("\u2018\u2019\u201c\u201d", "''\"\"")


def _quote(answer: str, text: str) -> str:
    """``text`` as the answer writes it: curly or straight quotes alike, and markdown emphasis
    (``*``) skipped, which models leave out of quotes ("wind 14 km/h" in "**wind 14 km/h**")."""
    kept = [i for i, char in enumerate(answer) if char != "*"]
    plain = "".join(answer[i] for i in kept).translate(_QUOTES)
    wanted = text.replace("*", "").translate(_QUOTES)
    start = plain.find(wanted) if wanted else -1
    if start < 0:
        raise ValueError("text is not quoted from the answer")
    return answer[kept[start] : kept[start + len(wanted) - 1] + 1]


def _request(answer: str, calls: list[tuple[str, str]]) -> str:
    responses = "\n\n".join(f"call: call_{i}\nurl: {url}\nresponse: {text}" for i, (url, text) in enumerate(calls, 1))
    return f"## Responses\n\n{responses}\n\n## Answer\n\n{answer}"
