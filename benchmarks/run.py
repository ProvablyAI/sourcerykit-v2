"""Verifier benchmark: does SourceryKit give the right verdict for each claim?

Runs every case in benchmarks/cases/ offline against an engine and compares its
verdict with the hand-labelled gold verdict. No network, no LLM, no backend: the
recorded responses are frozen in the case files (or in the agent run they point to).

    uv run python benchmarks/run.py                 # every linker in benchmarks/links/
    uv run python benchmarks/run.py linker:<model>  # the claims one linker wrote (link.py)

The claims' queries run on a PostgreSQL facts table, by default a local one:

    docker run -d --name sourcerykit-bench -e POSTGRES_PASSWORD=bench -p 55432:5432 postgres:17
"""

import asyncio
import json
import os
import sys
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg

from sourcerykit import linker
from sourcerykit.claims import Claim, verify
from sourcerykit.facts import Fact, flatten

BENCH_DIR = Path(__file__).parent
LINKS_DIR = BENCH_DIR / "links"
GOLD = {"supported", "contradicted", "unsupported", "uncheckable"}
BAD = {"contradicted", "unsupported"}  # claims a verifier must flag
COLUMNS = ("correct", "false_pass", "false_caught", "missed", "abstained")
DATABASE_URL = os.environ.get("BENCH_DATABASE_URL", "postgresql://postgres:bench@localhost:55432/postgres")
FACTS_COLUMNS = """call_ref uuid NOT NULL, path text NOT NULL, pattern text NOT NULL, idx integer, obj text,
    type text NOT NULL, num numeric, text text"""

Verdicts = Iterator[tuple[dict[str, Any], dict[str, Any], str | None]]


def load_cases() -> list[dict[str, Any]]:
    cases = [json.loads(p.read_text()) for p in sorted((BENCH_DIR / "cases").glob("*.json"))]
    for case in cases:
        if "run" in case:  # a frozen agent run from benchmarks/capture.py
            case.update(json.loads((BENCH_DIR / "runs" / f"{case['run']}.json").read_text()) | {"id": case["id"]})
        for claim in case["claims"]:
            where = f"{case['id']}: {claim['text']!r}"
            assert claim["text"] in case["answer"], f"{where} is not quoted verbatim from the answer"
            assert claim["gold_verdict"] in GOLD, f"{where} has unknown gold_verdict"
            v2 = claim.get("v2_claim")
            if v2 is not None:
                for step in v2.get("steps", {}).values():
                    calls = step["call"] if isinstance(step["call"], list) else [step["call"]]
                    step["call"] = [str(_call_uuid(_call_ref(case, call))) for call in calls]
                claim["v2_claim"] = Claim.model_validate({"text": claim["text"], **v2})
    return cases


def _call_ref(case: dict[str, Any], prefix: str) -> str:
    """The one call_ref of the case starting with ``prefix`` (real runs have long UUIDs)."""
    (call_ref,) = [c["call_ref"] for c in case["calls"] if c["call_ref"].startswith(prefix)]
    return str(call_ref)


def _call_uuid(call_ref: str) -> uuid.UUID:
    """Hand-written cases name calls r1, r2...: the facts table needs a UUID."""
    return uuid.uuid5(uuid.NAMESPACE_URL, call_ref)


def score(gold: str, verdict: str | None) -> str:
    """Classifies one engine verdict against the gold label."""
    if gold in BAD:  # must be flagged
        return {"contradicted": "correct", "supported": "false_pass", None: "missed"}[verdict]
    if gold == "supported":
        return {"supported": "correct", "contradicted": "false_caught", None: "abstained"}[verdict]
    # uncheckable (opinion, not a fact): saying nothing is right
    return {None: "correct", "supported": "false_pass", "contradicted": "false_caught"}[verdict]


def load_facts(
    db: psycopg.Connection[Any], case: dict[str, Any], table: str = "facts"
) -> list[tuple[uuid.UUID, list[Fact]]]:
    """Replaces ``table`` with the facts of the case's responses; returns each call's facts."""
    db.execute(f"DROP TABLE IF EXISTS {table}")
    db.execute(f"CREATE TABLE {table} ({FACTS_COLUMNS})")
    calls = []
    for call in case["calls"]:
        rows = flatten(_call_uuid(call["call_ref"]), json.dumps(call["response"]))
        assert rows is not None, f"{case['id']}: {call['call_ref']} has no facts"
        with db.cursor() as cursor:
            cursor.executemany(
                f"INSERT INTO {table} VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [(r.call_ref, r.path, r.pattern, r.idx, r.obj, r.type, r.num, r.text) for r in rows],
            )
        calls.append((_call_uuid(call["call_ref"]), rows))
    return calls


Claims = Callable[[dict[str, Any], list[tuple[uuid.UUID, list[Fact]]]], tuple[list[Claim], list[str]]]


def linker_claims(model: str) -> Claims:
    """The claims the linker wrote for each case, in its frozen reply."""

    def claims(case: dict[str, Any], calls: list[tuple[uuid.UUID, list[Fact]]]) -> tuple[list[Claim], list[str]]:
        reply = json.loads((LINKS_DIR / model / f"{case['id']}.json").read_text())["reply"]
        paths = {p for _, facts in calls for f in facts for p in (f.path, f.pattern)}
        claims, errors = linker.parse(case["answer"], reply, [call for call, _ in calls], paths)
        skipped = [f"UNCHECKED {text!r} at {at}" for at, text in linker.unchecked(case["answer"], claims)]
        return claims, errors + skipped

    return claims


def claimed_verdicts(cases: list[dict[str, Any]], make_claims: Claims, notes: list[str]) -> Verdicts:
    """v2 on claims an engine found in the answer: each statement gets the verdicts of the claims quoting it.

    A claim may quote more than one statement ("departs at 09:40 and costs €99"). A bad statement
    is caught if any claim overlapping it is. A good one is falsely caught only by a caught claim
    that overlaps no bad statement, which its CAUGHT would otherwise be about. Claims overlapping
    no statement, and malformed ones, go to ``notes``.
    """
    with psycopg.connect(DATABASE_URL, autocommit=True) as db:

        async def query(sql: str) -> list[Any]:
            return [row[0] for row in db.execute(sql)]

        for case in cases:
            answer = case["answer"]
            claims, errors = make_claims(case, load_facts(db, case))
            notes.extend(
                f"  {case['id']}: {error}" if error.startswith("UNCHECKED") else f"  MALFORMED  {case['id']}: {error}"
                for error in errors
            )
            linked = [(_span(answer, c.text), c.text, asyncio.run(verify(c, query))[0]) for c in claims]
            statements = [(_span(answer, s["text"]), s) for s in case["claims"]]
            bad = [span for span, s in statements if s["gold_verdict"] in BAD]
            matched: set[int] = set()
            for span, statement in statements:
                over = [i for i, (s, _, _) in enumerate(linked) if _overlap(s, span)]
                matched.update(over)
                caught = [linked[i][0] for i in over if linked[i][2] != "supported"]
                if not over:
                    yield case, statement, None
                elif statement["gold_verdict"] in BAD:
                    yield case, statement, "contradicted" if caught else "supported"
                elif any(not any(_overlap(c, b) for b in bad) for c in caught):
                    yield case, statement, "contradicted"
                else:
                    yield case, statement, "supported"
            notes.extend(
                f"  EXTRA {verdict:12} {case['id']}: {text!r}"
                for i, (_, text, verdict) in enumerate(linked)
                if i not in matched
            )


def _span(answer: str, text: str) -> tuple[int, int]:
    start = answer.index(text)
    return start, start + len(text)


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def report(engine: str, cases: list[dict[str, Any]], verdicts: Verdicts, notes: list[str] | None = None) -> None:
    by_category: dict[str, Counter[str]] = defaultdict(Counter)
    problems: list[str] = []
    for case, claim, verdict in verdicts:
        result = score(claim["gold_verdict"], verdict)
        by_category[claim["category"]][result] += 1
        if result != "correct":
            problems.append(f"  {result.upper():13} {case['id']}: {claim['text']!r}  ({claim.get('note', '')})")

    total: Counter[str] = sum(by_category.values(), Counter())
    print(f"engine: {engine}   cases: {len(cases)}   claims: {total.total()}\n")
    print(f"{'category':14}" + "".join(f"{c:>14}" for c in COLUMNS))
    for name, counts in sorted(by_category.items()) + [("TOTAL", total)]:
        print(f"{name:14}" + "".join(f"{counts[c]:>14}" for c in COLUMNS))
    if problems:
        print("\nnot correct:")
        print("\n".join(problems))
    if notes:
        print("\nclaims that are malformed, or quote no gold statement:")
        print("\n".join(notes))


def main() -> int:
    cases = load_cases()
    linkers = [f"linker:{path.name}" for path in sorted(LINKS_DIR.glob("*"))]
    for engine in sys.argv[1:] or linkers:
        notes: list[str] = []
        make_claims = linker_claims(engine.removeprefix("linker:"))
        report(engine, cases, claimed_verdicts(cases, make_claims, notes), notes)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
