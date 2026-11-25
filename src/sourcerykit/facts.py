"""Flatten a recorded JSON response into rows of the fixed-schema ``facts`` table.

Each JSON leaf becomes one row, and each array one extra row holding its length, so that
claims about any response are queries on the same columns. qedb proves queries on one table
with filters against constants, so every column below is a plain value that a claim filters
on or aggregates over:

- ``path``: exact location of the value, e.g. ``$.flights[2].price``.
- ``pattern``: the path with array indices replaced by ``[*]``, e.g. ``$.flights[*].price``,
  to select a whole series.
- ``idx``: index in the innermost enclosing array, to select a slice of a series.
- ``obj``: path of the innermost enclosing array element, e.g. ``$.flights[2]``, to relate
  fields of the same element ("the cheapest BA flight").
- ``type``: ``number``, ``string``, ``bool``, ``null`` or ``array``.
- ``num``: the numeric value (booleans as 1 or 0, arrays as their length), set only when it
  fits qedb's decimal encoding: at most 15 decimal digits and magnitude below 10^23.
- ``text``: the value as canonical text, for exact equality; numbers in plain notation
  without trailing zeros (``187.30`` and ``1.873e2`` are both ``187.3``).

Numbers are parsed exactly from the response text, never through a float, and never rounded:
a proof about a rounded value would be a proof about a different number. Rows follow the
order of the JSON text, so the same response always yields the same rows in the same order,
which the table commitment depends on.
"""

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

MAX_ROWS = 10_000

_MAX_DECIMALS = 15
_MAX_MAGNITUDE = Decimal(10) ** 23
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Fact:
    """One row of the facts table: its fields are the table's columns."""

    call_ref: UUID
    path: str
    pattern: str
    idx: int | None
    obj: str | None
    type: str
    num: Decimal | None
    text: str | None


class _TooManyRows(Exception):
    pass


def flatten(call_ref: UUID, raw: str) -> list[Fact] | None:
    """Rows for one recorded response, or None if it is not JSON or exceeds ``MAX_ROWS``.

    A response over the cap gets no rows at all rather than some: an aggregate proven over
    part of a series would be a proven wrong value.
    """
    try:
        value = json.loads(raw, parse_float=Decimal, parse_constant=_reject_constant)
    except ValueError:
        return None
    rows: list[Fact] = []
    try:
        _walk(call_ref, value, "$", "$", None, None, rows)
    except _TooManyRows:
        return None
    return rows


def canonical_number(value: int | Decimal) -> str:
    """Plain decimal notation, no exponent and no trailing zeros; ``-0`` is ``0``."""
    if isinstance(value, int):
        return str(value)
    text = format(value.normalize(), "f")
    return "0" if text == "-0" else text


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _walk(
    call_ref: UUID, value: Any, path: str, pattern: str, idx: int | None, obj: str | None, rows: list[Fact]
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            step = _key_step(key)
            _walk(call_ref, child, path + step, pattern + step, idx, obj, rows)
        return
    if isinstance(value, list):
        _add(rows, Fact(call_ref, path, pattern, idx, obj, "array", Decimal(len(value)), str(len(value))))
        for i, child in enumerate(value):
            _walk(call_ref, child, f"{path}[{i}]", f"{pattern}[*]", i, f"{path}[{i}]", rows)
        return
    if isinstance(value, bool):  # before int: bool is a subclass of int
        _add(rows, Fact(call_ref, path, pattern, idx, obj, "bool", Decimal(int(value)), "true" if value else "false"))
    elif isinstance(value, (int, Decimal)):
        number = Decimal(value)
        _add(rows, Fact(call_ref, path, pattern, idx, obj, "number", _fits(number), canonical_number(value)))
    elif isinstance(value, str):
        _add(rows, Fact(call_ref, path, pattern, idx, obj, "string", None, value))
    else:  # None
        _add(rows, Fact(call_ref, path, pattern, idx, obj, "null", None, None))


def _key_step(key: str) -> str:
    """``.key`` for identifier keys, else ``['key']`` with backslashes and quotes escaped."""
    if _IDENTIFIER.fullmatch(key):
        return f".{key}"
    return "['" + key.replace("\\", "\\\\").replace("'", "\\'") + "']"


def _fits(number: Decimal) -> Decimal | None:
    """The number if qedb's decimal encoding holds it exactly, else None."""
    normalized = number.normalize()
    exponent = normalized.as_tuple().exponent
    if not isinstance(exponent, int) or -exponent > _MAX_DECIMALS or abs(normalized) >= _MAX_MAGNITUDE:
        return None
    return number


def _add(rows: list[Fact], fact: Fact) -> None:
    if len(rows) >= MAX_ROWS:
        raise _TooManyRows
    rows.append(fact)
