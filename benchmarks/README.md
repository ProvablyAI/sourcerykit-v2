# Verifier benchmark

Measures whether SourceryKit gives the **right verdict** for each statement in an agent's
answer. Runs offline: the recorded tool responses are frozen in the case files, so scoring
needs no network, LLM or backend.

```bash
docker run -d --name sourcerykit-bench -e POSTGRES_PASSWORD=bench -p 55432:5432 postgres:17
uv run python benchmarks/run.py          # every engine
uv run python benchmarks/run.py v1       # v1 only, no database needed
```

Linker engines load each case's responses into a PostgreSQL facts table (`BENCH_DATABASE_URL`,
by default the container above) and run each claim's queries on it.

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
- `v2_claim`: the claim a perfect extractor would write for it (`sourcerykit.claims.Claim`
  without `text`), or `null` for no verdict. A step's `call` is a call_ref, or the start of one.
  These hand-written annotations are reference data, validated on load and not scored as an
  engine: what each statement means, as a claim. A stated unit or currency is
  checked against the response, and is `unsupported` when the response has none.

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

## Linker runs

`link.py` runs the linker (`sourcerykit.linker`: one LLM call per answer, turning its statements
into claims) on every case, and freezes the replies in `links/<model>/`. `run.py` scores each
frozen run as engine `linker:<model>`, offline: the claims run on the facts table, and each
statement gets the verdicts of the claims quoting it.

```bash
MODEL_URL=... MODEL_API_KEY=... uv run python benchmarks/link.py anthropic/claude-haiku-5.5
uv run python benchmarks/run.py linker:anthropic_claude-haiku-5.5
```

A claim may quote two statements at once: a CAUGHT counts against a good statement only when the
claim quotes no bad one. Claims that are malformed, or quote no gold statement, are listed after
the scores for review. Malformed includes claims the linker's guards reject: a number in the quote
that no check or filter uses, a tolerance wider than half a unit of the stated value, or a number
in an expression that does more than scale a unit. `UNCHECKED` lines are numbers, times and dates
of the answer that no claim quotes.
