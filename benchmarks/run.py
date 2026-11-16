"""Verifier benchmark: does SourceryKit give the right verdict for each claim?

Runs every case in benchmarks/cases/ offline against an engine and compares its
verdict with the hand-labelled gold verdict. No network, no LLM, no backend: the
recorded responses are frozen in the case files.

    uv run python benchmarks/run.py
"""

import json
import sys
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from sourcerykit.evaluator._eval_modes import evaluate_claim
from sourcerykit.schemas import HandoffClaim, VerificationMode

CASES_DIR = Path(__file__).parent / "cases"
GOLD = {"supported", "contradicted", "uncheckable"}


def load_cases() -> list[dict[str, Any]]:
    cases = [json.loads(p.read_text()) for p in sorted(CASES_DIR.glob("*.json"))]
    for case in cases:
        refs = {c["call_ref"] for c in case["calls"]}
        for claim in case["claims"]:
            where = f"{case['id']}: {claim['text']!r}"
            assert claim["text"] in case["answer"], f"{where} is not quoted verbatim from the answer"
            assert claim["gold_verdict"] in GOLD, f"{where} has unknown gold_verdict"
            v1 = claim.get("v1_claim")
            assert v1 is None or v1["call_ref"] in refs, f"{where} points to an unknown call_ref"
    return cases


def run_v1(claim: dict[str, Any], responses: dict[str, Any]) -> str | None:
    """Verdict of the v1 engine: 'supported', 'contradicted', or None if it can't express the claim."""
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
    if verdict is None:
        # Abstaining is the right answer for a claim nothing in the trace can settle.
        return "correct" if gold == "uncheckable" else "abstained"
    if verdict == gold:
        return "correct"
    return "false_pass" if verdict == "supported" else "false_caught"


def main() -> int:
    cases = load_cases()
    by_category: dict[str, Counter[str]] = defaultdict(Counter)
    wrong: list[str] = []
    checkable = covered = 0

    for case in cases:
        responses = {c["call_ref"]: c["response"] for c in case["calls"]}
        for claim in case["claims"]:
            verdict = run_v1(claim, responses)
            result = score(claim["gold_verdict"], verdict)
            by_category[claim["category"]][result] += 1
            if claim["gold_verdict"] != "uncheckable":
                checkable += 1
                covered += verdict is not None
            if result in ("false_pass", "false_caught"):
                wrong.append(f"  {result.upper():13} {case['id']}: {claim['text']!r}  ({claim.get('note', '')})")

    total: Counter[str] = sum(by_category.values(), Counter())
    cols = ("correct", "false_pass", "false_caught", "abstained")
    print(f"engine: v1   cases: {len(cases)}   claims: {total.total()}\n")
    print(f"{'category':14}" + "".join(f"{c:>14}" for c in cols))
    for name, counts in sorted(by_category.items()) + [("TOTAL", total)]:
        print(f"{name:14}" + "".join(f"{counts[c]:>14}" for c in cols))

    print(f"\ncoverage: {covered}/{checkable} checkable claims got a verdict")
    if wrong:
        print("\nwrong verdicts:")
        print("\n".join(wrong))
    return 0


if __name__ == "__main__":
    sys.exit(main())
