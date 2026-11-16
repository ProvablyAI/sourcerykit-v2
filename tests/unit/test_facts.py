"""Tests for sourcerykit.facts."""

import json
import re
import uuid
from decimal import Decimal
from typing import Any

import pytest

from sourcerykit import facts
from sourcerykit.facts import Fact, flatten

_REF = uuid.uuid4()

_FLIGHTS = json.dumps(
    {
        "route": {"from": "LHR", "to": "FCO"},
        "flights": [
            {"number": "AZ205", "airline": "ITA", "price": 95},
            {"number": "BA117", "airline": "BA", "price": 140},
        ],
    }
)


def _rows(raw: str) -> dict[str, Fact]:
    rows = flatten(_REF, raw)
    assert rows is not None
    return {r.path: r for r in rows}


def _read(value: Any, path: str) -> Any:
    """Independent reader for the paths flatten writes: .key, ['key'] and [index]."""
    for dotted, quoted, index in re.findall(r"\.([A-Za-z_][A-Za-z0-9_]*)|\['((?:\\.|[^'\\])*)'\]|\[(\d+)\]", path[1:]):
        if index:
            value = value[int(index)]
        else:
            value = value[dotted or re.sub(r"\\(.)", r"\1", quoted)]
    return value


class TestFlatten:
    def test_array_of_objects(self) -> None:
        rows = _rows(_FLIGHTS)
        assert rows["$.flights"].type == "array"
        assert rows["$.flights"].num == 2
        price = rows["$.flights[1].price"]
        assert (price.pattern, price.idx, price.obj, price.num, price.text) == (
            "$.flights[*].price",
            1,
            "$.flights[1]",
            Decimal(140),
            "140",
        )
        assert rows["$.route.from"] == Fact(_REF, "$.route.from", "$.route.from", None, None, "string", None, "LHR")

    def test_every_row_reads_back_its_value(self) -> None:
        doc = {
            "a": [[1, 2], [], {"b": None}],
            "wind.speed": 3.5,
            "it's": {"x\\y": True},
            "s": "42",
        }
        raw = json.dumps(doc)
        rows = flatten(_REF, raw)
        assert rows is not None
        parsed = json.loads(raw, parse_float=Decimal)
        for row in rows:
            value = _read(parsed, row.path)
            if row.type == "array":
                assert row.num == len(value)
            elif row.type == "bool":
                assert row.text == ("true" if value else "false")
            elif row.type == "number":
                assert Decimal(row.text or "") == value
            else:
                assert row.text == value
        assert rows == flatten(_REF, raw)  # deterministic

    def test_nested_array_uses_innermost_element(self) -> None:
        rows = _rows('{"a": [[10, 20]]}')
        inner = rows["$.a[0][1]"]
        assert (inner.pattern, inner.idx, inner.obj) == ("$.a[*][*]", 1, "$.a[0][1]")
        assert rows["$.a[0]"].type == "array"

    def test_empty_array_has_length_zero(self) -> None:
        assert _rows('{"a": []}')["$.a"].num == 0


class TestNumbers:
    @pytest.mark.parametrize(
        ("raw", "text", "num"),
        [
            ("187.30", "187.3", Decimal("187.3")),
            ("1e-05", "0.00001", Decimal("0.00001")),
            ("1.873e2", "187.3", Decimal("187.3")),
            ("-1.25", "-1.25", Decimal("-1.25")),
            ("-0.0", "0", Decimal(0)),
            ("123456789012345678901234567", "123456789012345678901234567", None),
            ("0.1234567890123456", "0.1234567890123456", None),
        ],
    )
    def test_canonical_text_and_qedb_range(self, raw: str, text: str, num: Decimal | None) -> None:
        row = _rows(f'{{"v": {raw}}}')["$.v"]
        assert (row.type, row.text, row.num) == ("number", text, num)

    def test_bool_and_numeric_string(self) -> None:
        rows = _rows('{"b": false, "s": "42"}')
        assert (rows["$.b"].type, rows["$.b"].num, rows["$.b"].text) == ("bool", Decimal(0), "false")
        assert (rows["$.s"].type, rows["$.s"].num) == ("string", None)


class TestNoRows:
    @pytest.mark.parametrize("raw", ["<html>not json</html>", '{"v": NaN}', ""])
    def test_not_json(self, raw: str) -> None:
        assert flatten(_REF, raw) is None

    def test_over_the_cap_gets_no_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(facts, "MAX_ROWS", 3)
        assert flatten(_REF, "[1, 2]") is not None  # array row + 2 leaves
        assert flatten(_REF, "[1, 2, 3]") is None
