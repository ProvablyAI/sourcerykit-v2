"""Verifier benchmark: does SourceryKit give the right verdict for each claim?

Runs every case in benchmarks/cases/ offline against an engine and compares its
verdict with the hand-labelled gold verdict. No network, no LLM, no backend: the
recorded responses are frozen in the case files (or in the agent run they point to).

    uv run python benchmarks/run.py          # both engines
    uv run python benchmarks/run.py v1       # v1 only, no database needed

The v2 engine runs its queries on a PostgreSQL facts table, by default a local one:

    docker run -d --name sourcerykit-bench -e POSTGRES_PASSWORD=bench -p 55432:5432 postgres:17
"""

import json
import os
import sys
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg

from sourcerykit.claims import Claim, verify
from sourcerykit.evaluator._eval_modes import evaluate_claim
from sourcerykit.facts import flatten
from sourcerykit.schemas import HandoffClaim, VerificationMode

BENCH_DIR = Path(__file__).parent
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
        refs = {c["call_ref"] for c in case["calls"]}
        for claim in case["claims"]:
            where = f"{case['id']}: {claim['text']!r}"
            assert claim["text"] in case["answer"], f"{where} is not quoted verbatim from the answer"
            assert claim["gold_verdict"] in GOLD, f"{where} has unknown gold_verdict"
            v1 = claim.get("v1_claim")
            if v1 is not None and "declared_ref" in v1:
                v1.update(_declared_group(case, v1["declared_ref"]))
            assert v1 is None or v1["call_ref"] in refs, f"{where} points to an unknown call_ref"
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


def _declared_group(case: dict[str, Any], ref_prefix: str) -> dict[str, Any]:
    """v1 checks all values the agent declared for one call together, as build_handoff_payload groups them."""
    call_ref = _call_ref(case, ref_prefix)
    group = [d for d in case["declared_claims"] if d["sourcerykit_ref"] == call_ref]
    return {"call_ref": call_ref, "verification_mode": "field_extraction", "claimed_value": group}


def run_v1(claim: dict[str, Any], responses: dict[str, Any]) -> str | None:
    """Verdict of the v1 engine: 'supported', 'contradicted', or None if it does not check the claim."""
    spec = claim.get("v1_claim")
    if spec is None:
        return None
    handoff_claim = HandoffClaim(
        action_name="bench",
        query_id=uuid.uuid4(),
        verification_mode=VerificationMode(spec["verification_mode"]),
        claimed_value=spec["claimed_value"],
        json_path=spec.get("json_path", ""),
        range_min=spec.get("range_min"),
        range_max=spec.get("range_max"),
    )
    outcome = evaluate_claim(handoff_claim, responses[spec["call_ref"]])["result"]
    return {"PASS": "supported", "CAUGHT": "contradicted"}.get(outcome)


def score(gold: str, verdict: str | None) -> str:
    """Classifies one engine verdict against the gold label."""
    if gold in BAD:  # must be flagged
        return {"contradicted": "correct", "supported": "false_pass", None: "missed"}[verdict]
    if gold == "supported":
        return {"supported": "correct", "contradicted": "false_caught", None: "abstained"}[verdict]
    # uncheckable (opinion, not a fact): saying nothing is right
    return {None: "correct", "supported": "false_pass", "contradicted": "false_caught"}[verdict]


def v1_verdicts(cases: list[dict[str, Any]]) -> Verdicts:
    for case in cases:
        responses = {c["call_ref"]: c["response"] for c in case["calls"]}
        for claim in case["claims"]:
            yield case, claim, run_v1(claim, responses)


def v2_verdicts(cases: list[dict[str, Any]]) -> Verdicts:
    """Each case's responses go into the facts table, then each claim's queries run on it."""
    with psycopg.connect(DATABASE_URL, autocommit=True) as db:

        def query(sql: str) -> list[Any]:
            return [row[0] for row in db.execute(sql)]

        for case in cases:
            load_facts(db, case)
            for claim in case["claims"]:
                spec = claim.get("v2_claim")
                verdict = None if spec is None else verify(spec, query)[0]
                yield case, claim, "contradicted" if verdict == "unsupported" else verdict  # both are CAUGHT


def load_facts(db: psycopg.Connection[Any], case: dict[str, Any], table: str = "facts") -> None:
    """Replaces ``table`` with the facts of the case's responses."""
    db.execute(f"DROP TABLE IF EXISTS {table}")
    db.execute(f"CREATE TABLE {table} ({FACTS_COLUMNS})")
    for call in case["calls"]:
        rows = flatten(_call_uuid(call["call_ref"]), json.dumps(call["response"]))
        assert rows is not None, f"{case['id']}: {call['call_ref']} has no facts"
        with db.cursor() as cursor:
            cursor.executemany(
                f"INSERT INTO {table} VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [(r.call_ref, r.path, r.pattern, r.idx, r.obj, r.type, r.num, r.text) for r in rows],
            )


ENGINES = {"v1": v1_verdicts, "v2": v2_verdicts}


def report(engine: str, cases: list[dict[str, Any]], verdicts: Verdicts) -> None:
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


def main() -> int:
    cases = load_cases()
    for engine in sys.argv[1:] or list(ENGINES):
        report(engine, cases, ENGINES[engine](cases))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
