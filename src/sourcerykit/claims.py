"""Claims about recorded responses, checked by queries on the facts table.

A claim is a chain of steps and a list of checks. Each step is one SQL query on the facts table
(see ``sourcerykit.facts``) in the subset qedb proves: one table, filters of a column against
constants. A later step can filter on what an earlier one returned, so "the cheapest BA flight"
is the objects whose airline is BA, then the minimum price among those objects. Steps run in
order, and each one's constants are fixed by the answers before it, so anyone holding the proven
answers can rebuild every query.

The checks compare the step answers with the values the answer text states. qedb proves the
values; the comparison is plain arithmetic anyone can redo, which lets a claim allow a tolerance
("about 6.33") or compare two calls ("3 degrees warmer than yesterday"), and lets a contradicted
claim show the recorded value.

A claim the recorded data cannot back at all (a currency the response never mentions) carries
``unsupported`` with the reason instead of steps.
"""

import ast
import operator
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, time
from decimal import ROUND_HALF_EVEN, Context, Decimal, DivisionByZero, InvalidOperation, Overflow, localcontext
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sourcerykit.facts import canonical_number

Verdict = Literal["supported", "contradicted", "unsupported"]
Query = Callable[[str], Awaitable[list[Any]]]
"""Runs one SQL query and returns the first column of every row."""

_TEXT_COLUMNS = {"path", "pattern", "obj", "type", "text"}
_NUMBER_COLUMNS = {"idx", "num"}
_AGGREGATES = {"count": "COUNT(*)", "min": "MIN(num)", "max": "MAX(num)", "sum": "SUM(num)", "avg": "AVG(num)"}
_FILTER_OPS = {"=", "!=", "<", "<=", ">", ">=", "between", "in"}
_COMPARE = {
    "=": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}
_ARITHMETIC = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# The checks' arithmetic, fixed here: Python's decimal context is per thread and the host
# application may change it, which would change verdicts.
_ARITHMETIC_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, DivisionByZero, Overflow])

Column = Literal["path", "pattern", "idx", "obj", "type", "num", "text"]


class Step(BaseModel):
    """One query on the facts table.

    ``where`` maps a column to a constant (equality), to ``{op: value}``, or to ``{"step": name}``
    for the answer of an earlier step; ``{"in": {"step": name}}`` filters on all its values.
    """

    model_config = ConfigDict(extra="forbid")

    call: list[UUID] = Field(min_length=1, description="Recorded calls the query reads.")
    select: Literal["text", "num", "obj", "idx", "count", "min", "max", "sum", "avg"]
    where: dict[Column, Any] = Field(default_factory=dict)

    @field_validator("call", mode="before")
    @classmethod
    def _one_or_more(cls, value: Any) -> Any:
        return value if isinstance(value, list) else [value]

    @field_validator("where")
    @classmethod
    def _valid_filters(cls, where: dict[str, Any]) -> dict[str, Any]:
        for column, value in where.items():
            op, operand = _filter(value)
            if _step_ref(operand) is None:
                for constant in operand if op in ("between", "in") else [operand]:
                    _literal(column, constant)
        return where


class Check(BaseModel):
    """``expr`` is a step name, or ``+ - * /`` over step names and numbers."""

    model_config = ConfigDict(extra="forbid")

    expr: str
    op: Literal["=", "!=", "<", "<=", ">", ">=", "between", "approx"]
    value: Any
    tol: Decimal | None = None

    @model_validator(mode="after")
    def _valid(self) -> "Check":
        if not _names(self.expr):  # "99" = 99 would hold whatever the data says
            raise ValueError(f"check {self.expr!r} reads no step")
        if self.op == "approx" and (self.tol is None or self.tol < 0):
            raise ValueError("approx needs a tolerance tol >= 0")
        if self.op == "between":
            if not (isinstance(self.value, list) and len(self.value) == 2):
                raise ValueError("between needs [low, high]")
            for bound in self.value:
                _number(bound)
        elif isinstance(self.value, (str, bool)) or self.value is None:
            if self.op not in ("=", "!="):
                raise ValueError(f"{self.op} needs a number, not {self.value!r}")
        else:
            _number(self.value)
        return self


class Claim(BaseModel):
    """One statement of the answer, quoted in ``text``, and how to check it."""

    model_config = ConfigDict(extra="forbid")

    text: str
    steps: dict[str, Step] = Field(default_factory=dict)
    checks: list[Check] = Field(default_factory=list)
    unsupported: str | None = Field(default=None, description="Why the recorded data cannot back the claim.")

    @model_validator(mode="after")
    def _valid(self) -> "Claim":
        if self.unsupported is not None:
            if self.steps or self.checks:
                raise ValueError("an unsupported claim has no steps or checks")
            return self
        if not self.checks:
            raise ValueError("a claim needs checks, or unsupported with a reason")
        earlier: dict[str, Step] = {}
        for name, step in self.steps.items():
            if not _IDENTIFIER.fullmatch(name):
                raise ValueError(f"step name {name!r} is not an identifier")
            for column, value in step.where.items():
                ref = _step_ref(_filter(value)[1])
                if ref is None:
                    continue
                if ref not in earlier:
                    raise ValueError(f"step {name!r} refers to {ref!r}, which is not an earlier step")
                if (column in _TEXT_COLUMNS) != (earlier[ref].select in _TEXT_COLUMNS):
                    raise ValueError(f"step {name!r} filters {column} on {ref!r}, which selects {earlier[ref].select}")
            earlier[name] = step
        for check in self.checks:
            unknown = _names(check.expr) - earlier.keys()
            if unknown:
                raise ValueError(f"check {check.expr!r} uses unknown steps {sorted(unknown)}")
        return self


async def evaluate(
    claim: Claim, query: Query, table: str = "facts"
) -> tuple[Verdict, dict[str, tuple[str | None, list[Any]]]]:
    """The claim's verdict, and each step's SQL with its answer.

    A step whose answer is known without running it (it filters on an empty or NULL answer, which
    selects no row) has no SQL. A claim whose steps do not resolve to the single values its checks
    need (a path that is not in the response, text that is not a number) is contradicted.
    """
    if claim.unsupported is not None:
        return "unsupported", {}
    answers: dict[str, list[Any]] = {}
    queries: dict[str, tuple[str | None, list[Any]]] = {}
    try:
        for name, step in claim.steps.items():
            statement = sql(step, answers, table)
            answers[name] = _no_rows(step) if statement is None else [_typed(step, v) for v in await query(statement)]
            queries[name] = (statement, answers[name])
        with localcontext(_ARITHMETIC_CONTEXT):
            holds = all(_holds(check, answers) for check in claim.checks)
    except _Unresolved:
        holds = False
    return ("supported" if holds else "contradicted"), queries


def sql(step: Step, answers: dict[str, list[Any]], table: str = "facts") -> str | None:
    """The step's query, or None if it selects no row whatever the data."""
    if not _IDENTIFIER.fullmatch(table):
        raise ValueError(f"table name {table!r} is not an identifier")
    calls = [str(call) for call in step.call]
    quoted = ", ".join(f"'{call}'" for call in calls)  # UUIDs: nothing to escape
    conditions = [f"call_ref = {quoted}" if len(calls) == 1 else f"call_ref IN ({quoted})"]
    for column, value in step.where.items():
        op, operand = _filter(value)
        ref = _step_ref(operand)
        if ref is not None:
            # Sorted: rows come back in no set order, and the same claim must give the same SQL.
            operand = sorted({v for v in answers[ref] if v is not None}, key=str)
            if op != "in":
                if not operand:
                    return None  # comparing with NULL selects nothing
                operand = _single(answers[ref])
        if op == "in":
            if not operand:
                return None
            conditions.append(f"{column} IN ({_list(column, operand)})")
        elif op == "between":
            low, high = operand
            conditions.append(f"{column} BETWEEN {_literal(column, low)} AND {_literal(column, high)}")
        else:
            conditions.append(f"{column} {'<>' if op == '!=' else op} {_literal(column, operand)}")
    if step.select in _AGGREGATES:
        # An array's own row holds its length in num: never what a minimum or count is about.
        conditions.append("type <> 'array'")
    select = _AGGREGATES.get(step.select, step.select)
    return f"SELECT {select} FROM {table} WHERE {' AND '.join(conditions)}"


class _Unresolved(ValueError):
    """The steps' answers cannot settle a check; while validating a claim, a bad constant."""


def _filter(value: Any) -> tuple[str, Any]:
    """``(op, operand)`` of one ``where`` entry."""
    if isinstance(value, dict) and _step_ref(value) is None:
        if len(value) != 1:
            raise ValueError(f"a filter has one operator: {value!r}")
        ((op, operand),) = value.items()
        if op not in _FILTER_OPS:
            raise ValueError(f"unknown filter operator {op!r}")
        if op in ("between", "in") and _step_ref(operand) is None:
            if not isinstance(operand, list) or not operand or (op == "between" and len(operand) != 2):
                raise ValueError(f"{op} needs a list: {operand!r}")
        if op == "between" and _step_ref(operand) is not None:
            raise ValueError("between needs constants")
        return op, operand
    return "=", value


def _step_ref(operand: Any) -> str | None:
    if isinstance(operand, dict) and operand.keys() == {"step"}:
        return str(operand["step"])
    return None


def _literal(column: str, value: Any) -> str:
    if column in _TEXT_COLUMNS:
        if not isinstance(value, str):
            raise ValueError(f"{column} is compared with text, not {value!r}")
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{column} is compared with a number, not {value!r}")
    return canonical_number(_number(value))


def _list(column: str, values: list[Any]) -> str:
    return ", ".join(_literal(column, v) for v in values)


def _no_rows(step: Step) -> list[Any]:
    """The SQL answer on no row: no values, a count of 0, or a NULL aggregate."""
    if step.select == "count":
        return [0]
    return [None] if step.select in _AGGREGATES else []


def _typed(step: Step, value: Any) -> Any:
    """``value`` as the step's column holds it, whatever the database or prover returned it as."""
    if value is None or step.select in ("text", "obj"):
        return value
    number = Decimal(str(value))
    return int(number) if step.select in ("idx", "count") else number


def _single(answer: list[Any]) -> Any:
    if len(answer) != 1:
        raise _Unresolved(f"expected one value, got {len(answer)}")
    return answer[0]


def _number(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise _Unresolved(f"{value!r} is not a number")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise _Unresolved(f"{value!r} is not a number") from None
    if not number.is_finite():
        raise _Unresolved(f"{value!r} is not a number")
    return number


def _names(expr: str) -> set[str]:
    """Step names in ``expr``, which may only hold names, numbers, + - * / and parentheses."""
    if _IDENTIFIER.fullmatch(expr):  # a step name, even one Python reserves, such as "from"
        return {expr}
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"{expr!r} is not an expression: {e.msg}") from None
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.BinOp):
            if type(node.op) not in _ARITHMETIC:
                raise ValueError(f"only + - * / are allowed in {expr!r}")
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError(f"only numbers are allowed in {expr!r}")
        elif not isinstance(node, (ast.Expression, ast.UnaryOp, ast.USub, ast.Load, *_ARITHMETIC)):
            raise ValueError(f"{type(node).__name__} is not allowed in {expr!r}")
    return names


def _evaluate(node: ast.expr, answers: dict[str, list[Any]]) -> Decimal:
    if isinstance(node, ast.Name):
        return _number(_single(answers[node.id]))
    if isinstance(node, ast.Constant):
        return _number(node.value)
    if isinstance(node, ast.UnaryOp):
        return -_evaluate(node.operand, answers)
    assert isinstance(node, ast.BinOp)  # _names allows nothing else
    try:
        return Decimal(_ARITHMETIC[type(node.op)](_evaluate(node.left, answers), _evaluate(node.right, answers)))
    except ArithmeticError:
        raise _Unresolved(f"cannot compute {ast.unparse(node)}") from None


def folded(text: str) -> str:
    """``text`` without case, whitespace or underscores: "On time", "ON_TIME" and "ontime" are
    the same text, as are "LHR → CDG" and "LHR→CDG"."""
    return "".join(text.split()).replace("_", "").casefold()


def _same_text(got: Any, want: Any) -> bool:
    """Equal text once folded ("Shipped" and "SHIPPED"), or the same date and time written two
    ISO ways ("2026-11-23 11:00" and "2026-11-23T11:00:00Z")."""
    if got == want:
        return True
    if not (isinstance(got, str) and isinstance(want, str)):
        return False
    if folded(got) == folded(want):
        return True
    moments = (_moment(got), _moment(want))
    if moments[0] is None or moments[1] is None or type(moments[0]) is not type(moments[1]):
        return False
    if (moments[0].tzinfo is None) != (moments[1].tzinfo is None):
        # One side gives no zone, as answers often do: the clock reading is what it states
        return moments[0].replace(tzinfo=None) == moments[1].replace(tzinfo=None)
    return moments[0] == moments[1]


_UTC_WORDS = (" UTC", " GMT")


def _moment(text: str) -> datetime | time | None:
    """The date and time ``text`` writes in ISO form; a trailing "Z", " UTC" or " GMT" is +00:00."""
    for word in _UTC_WORDS:
        if text.endswith(word):
            text = text.removesuffix(word) + "+00:00"
    for parse in (datetime.fromisoformat, time.fromisoformat):
        try:
            return parse(text)
        except ValueError:
            pass
    return None


def _holds(check: Check, answers: dict[str, list[Any]]) -> bool:
    step = check.expr if _IDENTIFIER.fullmatch(check.expr) else None
    want = check.value
    if isinstance(want, (str, bool)) or want is None:
        if step is None:
            raise _Unresolved("text is compared with a single step")
        got = _single(answers[step])
        if isinstance(want, bool):
            want = "true" if want else "false"
        return _same_text(got, want) == (check.op == "=")
    if step is not None:
        got = _number(_single(answers[step]))
    else:
        got = _evaluate(ast.parse(check.expr, mode="eval").body, answers)
    if check.op == "between":
        low, high = (_number(bound) for bound in want)
        return low <= got <= high
    if check.op == "approx":
        assert check.tol is not None  # Check requires it
        return abs(got - _number(want)) <= check.tol
    return bool(_COMPARE[check.op](got, _number(want)))
