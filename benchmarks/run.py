"""Verifier benchmark: does SourceryKit give the right verdict for each claim?

Runs every case in benchmarks/cases/ offline against an engine and compares its
verdict with the hand-labelled gold verdict. No network, no LLM, no backend: the
recorded responses are frozen in the case files (or in the agent run they point to).

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

BENCH_DIR = Path(__file__).parent
GOLD = {"supported", "contradicted", "unsupported", "uncheckable"}
BAD = {"contradicted", "unsupported"}  # claims a verifier must flag
COLUMNS = ("correct", "false_pass", "false_caught", "missed", "abstained")


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
    return cases


def _declared_group(case: dict[str, Any], ref_prefix: str) -> dict[str, Any]:
    """v1 checks all values the agent declared for one call together, as build_handoff_payload groups them."""
    (call_ref,) = [c["call_ref"] for c in case["calls"] if c["call_ref"].startswith(ref_prefix)]
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


def main() -> int:
    cases = load_cases()
    by_category: dict[str, Counter[str]] = defaultdict(Counter)
    problems: list[str] = []

    for case in cases:
        responses = {c["call_ref"]: c["response"] for c in case["calls"]}
        for claim in case["claims"]:
            result = score(claim["gold_verdict"], run_v1(claim, responses))
            by_category[claim["category"]][result] += 1
            if result != "correct":
                problems.append(f"  {result.upper():13} {case['id']}: {claim['text']!r}  ({claim.get('note', '')})")

    total: Counter[str] = sum(by_category.values(), Counter())
    print(f"engine: v1   cases: {len(cases)}   claims: {total.total()}\n")
    print(f"{'category':14}" + "".join(f"{c:>14}" for c in COLUMNS))
    for name, counts in sorted(by_category.items()) + [("TOTAL", total)]:
        print(f"{name:14}" + "".join(f"{counts[c]:>14}" for c in COLUMNS))
    if problems:
        print("\nnot correct:")
        print("\n".join(problems))
    return 0


if __name__ == "__main__":
    sys.exit(main())
