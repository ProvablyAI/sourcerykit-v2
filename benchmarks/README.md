# Verifier benchmark

Measures whether SourceryKit gives the **right verdict** for each statement in an agent's
answer. Runs offline: the recorded tool responses are frozen in the case files, so scoring
needs no network, LLM or backend.

```bash
docker run -d --name sourcerykit-bench -e POSTGRES_PASSWORD=bench -p 55432:5432 postgres:17
uv run python benchmarks/run.py          # both engines
uv run python benchmarks/run.py v1       # v1 only, no database needed
```

The v2 engine loads each case's responses into a PostgreSQL facts table (`BENCH_DATABASE_URL`,
by default the container above) and runs each claim's queries on it.

## Gold labels

| Label | Meaning | Right verdict |
|---|---|---|
| `supported` | The recorded data backs the statement | PASS |
| `contradicted` | The recorded data says otherwise | CAUGHT |
| `unsupported` | Stated as fact, but nothing in the recorded data backs it (an invented value) | CAUGHT |
| `uncheckable` | Not a fact (an opinion) | none |

## Results

| Result | Meaning |
|---|---|
| `correct` | The verdict matches the gold label |
| `false_pass` | A bad statement (or an opinion) got PASS. **The worst error.** |
| `false_caught` | A supported statement (or an opinion) got CAUGHT |
| `missed` | A bad statement got no verdict at all: the hallucination went through unchecked |
| `abstained` | A supported statement got no verdict: lost coverage, not a wrong verdict |

## Cases

One JSON file per case in `cases/`. Each claim has:

- `text`: quoted verbatim from the answer (the runner checks this),
- `category`: `exact`, `range`, `aggregate`, `cross_object`, `cross_call` or `opinion`,
- `gold_verdict`: one of the labels above, with a `note` when it is not obvious,
- `v1_claim`: how the v1 engine checks it, or `null` if v1 does not. Either a full claim
  (`call_ref`, `verification_mode`, `claimed_value`), or `{"declared_ref": "<call_ref prefix>"}`
  for a real run: v1 checks all values the agent declared for that call together, as
  `build_handoff_payload` groups them, so they share one verdict.
- `v2_claim`: how the v2 engine checks it (`sourcerykit.claims.Claim` without `text`), or `null`
  for no verdict. A step's `call` is a call_ref, or the start of one. These are hand-written: the
  claims a perfect linker would extract from the answer, so the v2 score measures the engine, not
  the linker. A stated unit or currency is checked against the response, and is `unsupported`
  when the response has none.

A case either holds its own `calls` and `answer` (hand-written), or points to a frozen agent
run with `"run": "<file in runs/>"`.

## Real agent runs

`capture.py` runs a real agent the way the cookbooks do (`SourceryKitAgentResponse` output,
`sourcerykit_ref` in the tool output) without the Provably backend, and freezes the tool
responses, the answer and the declared claims in `runs/`. Only the LLM calls cost money; the
runs are then scored offline any number of times, by any engine version.

```bash
MODEL_URL=... MODEL_API_KEY=... uv run python benchmarks/capture.py weather honest openai/gpt-5-nano
```

Scenarios: `weather` (live Open-Meteo), `flights` (fixed data). Conditions: `honest`, `tamper`
(the prompt tells the agent to change a value), `fault` (the tool returns an error).
