# Verifier benchmark

Measures whether SourceryKit gives the **right verdict** for each claim an agent makes, not
whether the agent is right. Runs offline: the recorded tool responses are frozen in the case
files, so there is no network, LLM or backend involved.

```bash
uv run python benchmarks/run.py
```

## What it reports

| Result | Meaning |
|---|---|
| `correct` | Verdict matches the gold label (abstaining is correct for `uncheckable` claims) |
| `false_pass` | Engine said supported, gold says contradicted or uncheckable. **The worst error.** |
| `false_caught` | Engine said contradicted, gold says supported or uncheckable |
| `abstained` | Engine could not express a checkable claim. Lowers coverage, never a wrong verdict. |

`coverage` is the share of checkable claims (gold `supported` or `contradicted`) that got a verdict.

## Case format

One JSON file per case in `cases/`:

- `calls`: the recorded responses, each with a `call_ref`.
- `answer`: the agent's final answer, as text.
- `claims`: each with
  - `text`: quoted verbatim from `answer` (the runner checks this),
  - `category`: `exact`, `range`, `aggregate`, `cross_object`, `cross_call` or `uncheckable`,
  - `gold_verdict`: `supported`, `contradicted` or `uncheckable`,
  - `note`: why, when it is not obvious,
  - `v1_claim`: the claim as the v1 engine takes it, or `null` if v1 cannot express it.
