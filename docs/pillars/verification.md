# Verification
Verification checks every statement of an agent's plain-text answer against the HTTP calls its tools made, and proves every value a verdict relies on. The result is a report: an outcome, each statement's verdict with its proven values, and what could not be checked. Anyone given the report can check it with `check(report)`, without a Provably account.

## Core Flow

```python
async with sourcerykit.run() as r:                    # 1. collect the calls recorded inside
    answer = ...                                      #    run the agent; take its final text answer

report = await r.verify(answer, model=LINKER_MODEL)   # 2-4. link, seal, prove
print(report)
problems = await sourcerykit.check(report)            # 5. [] when the report holds
```

1. **Collect.** `run()` collects the ids of the calls recorded inside the block by [`async_intercept_context`](https://sourcerykit.provably.ai/docs/pillars/interceptor), including those made by the tasks it starts: an agent's tools, a whole multi-agent pipeline.
2. **Link.** The linker model reads the answer and the recorded responses, and writes a claim for each factual statement: how to check it on the data, or why the data cannot check it.
3. **Seal.** The responses are flattened into rows of your project's `facts` table and sealed by the QEDB middleware, before any query runs.
4. **Prove.** Every query a claim needs runs through the middleware, which proves its result. Each claim's verdict is computed from the proven results.
5. **Check.** `check(report)` sends every proof to Provably's public verifier and recomputes the claims, verdicts and outcome from what it proves.

## `run()` and `verify()`

```python
async with sourcerykit.run() as r: ...
await r.verify(answer: str, *, model: str, base_url: str | None = None, api_key: str | None = None) -> Report
```

`model` is the linker model. `base_url` and `api_key` default to `SOURCERYKIT_LINKER_URL` and `SOURCERYKIT_LINKER_API_KEY`:

| `SOURCERYKIT_LINKER_URL` | The linker is called through | `model`, for example |
|---|---|---|
| `https://api.anthropic.com` | Claude's Messages API; install `sourcerykit[anthropic]`. The key may come from `ANTHROPIC_API_KEY` instead. | `claude-haiku-5-5` |
| any other URL | An OpenAI-compatible chat API at `<url>/chat/completions` | `anthropic/claude-haiku-5.5` on OpenRouter |

The linker call is not recorded, so it never becomes evidence. Pass the answer exactly as the user receives it: `verify` normalizes it to Unicode NFC with `\n` line ends, and the report keeps that text.

## The Linker
The linker only writes how to check a statement, for example "the temperature field equals 14, in °C"; it never decides whether the statement is true. A claim is:

```json
{"text": "It is 14°C", "steps": {"t": {"call": "call_1", "select": "num", "where": {"path": "$.current.temperature_2m"}},
                                  "u": {"call": "call_1", "select": "text", "where": {"path": "$.current_units.temperature_2m"}}},
 "checks": [{"expr": "t", "op": "approx", "value": 14, "tol": 0.5}, {"expr": "u", "op": "=", "value": "°C"}]}
```

or, when no response backs or refutes the statement, `{"text": "...", "unsupported": "<what the responses lack>"}`.

- A **step** is one query on the facts of one or more calls: a column (`text`, `num`, `obj`, `idx`) or an aggregate (`count`, `min`, `max`, `sum`, `avg`), with filters on the columns below.
- A **check** compares what the steps returned with what the answer states: `=`, `!=`, `<`, `<=`, `>`, `>=`, `between`, or `approx` with a tolerance. An expression may combine steps with `+ - * /`, so "Paris is 1.1 °C warmer" is checked from the two proven temperatures.
- Text compares ignoring case, spacing and underscores ("On time" is `ON_TIME`), and dates and times compare as moments ("14:30 UTC" is `14:30:00Z`).
- A statement that names the data's value in other words ("United States" for `US`) is not verifiable rather than contradicted; one that states a different value ("Delayed" where the data says `ON_TIME`) is checked.

Code guards drop a claim, and list it in the report as dropped, when:

- its text is not quoted from the answer, or it reads a call that was not recorded, or it filters on a path the data does not have;
- a number, date or time in the quote is not one the checks compare with: the linker cannot check 140 where the answer says €99;
- an `approx` tolerance is wider than half a unit of the stated value, or an expression could shift the recorded value onto the stated one.

Numbers, times and dates in the answer that no claim quotes are listed as unchecked. The linker is a model: two runs on the same answer can link it differently, but every verdict is computed from proven data, and every claim it writes is kept in the report.

## The Facts Table
Each recorded JSON response is flattened into rows, one per value and one per array:

| Column | Holds |
|---|---|
| `call_ref` | The recorded call (its intercept row id). |
| `path` | Exact location, e.g. `$.flights[2].price`. |
| `pattern` | The path with array indices as `[*]`, e.g. `$.flights[*].price`, to select a series. |
| `idx` | The index in the innermost array. |
| `obj` | The path of the innermost array element, e.g. `$.flights[2]`, to relate fields of one element. |
| `type` | `number`, `string`, `bool`, `null` or `array`. |
| `num` | The number; booleans as 1 or 0, arrays as their length. |
| `text` | The value as canonical text; numbers without trailing zeros. |

Numbers are parsed exactly, never through a float. A response that is not JSON, or that flattens to more than 10,000 rows, gives no facts: statements about it are not verifiable. Each project has one `facts` table, registered with the middleware by `sourcerykit init`. A verification holds a database lock while it fills, seals and proves it, so verifications of one project run one at a time.

## The Report

```text
Outcome: PASS_WITH_NOTES   (claims by linker:anthropic/claude-haiku-5.5)
  SUPPORTED      '5.7°C'  proven: temp=5.7000  unit=°C
  NOT VERIFIABLE 'in London'  the responses give only coordinates (latitude 51.51147, longitude -0.13078308) and no place name, so nothing states that the location is London
  NOT VERIFIABLE 'about 42.3°F'  no response gives a Fahrenheit value or unit; the only temperature unit given is °C
  SUPPORTED      'as of 17:15 GMT on 2026-11-24'  proven: time=2026-11-24T17:15  tz=GMT
```

`Report` is a Pydantic model; `report.model_dump_json()` gives what you hand to someone else.

| Field | Meaning |
|---|---|
| `outcome` | See [Outcomes](#outcomes). |
| `answer` | The answer as verified. |
| `claims` | Each claim with its `verdict` (`supported`, `contradicted` or `unsupported`) and, for each step, its `sql`, the proven `answer`, the `query_id` and the `proof` (base64). |
| `dropped` | Claims the linker wrote that the guards rejected, and why. |
| `unchecked` | Numbers, times and dates in the answer that no claim checks. |
| `untrusted` | Recorded calls left out of verification, when your project lists trusted endpoints and these are not among them. |
| `calls` | The recorded calls used as evidence, in the order the linker saw them. |
| `extractor`, `reply` | The linker model and its reply, as received, so its claims can be re-checked. |
| `org_id` | The Provably organization whose middleware made the proofs. |

## Outcomes

| Outcome | When |
|---|---|
| `PASS` | Every statement is checked and backed by the recorded data. |
| `PASS_WITH_NOTES` | Nothing is contradicted, but some statements have no data to check them; read them in the report. |
| `CAUGHT` | A statement is contradicted by the recorded data. |
| `ERROR` | No statement could be checked: nothing was verified. Verifying nothing is never a pass. |

To act on the outcome, route on `report.outcome`: the [LangGraph](https://sourcerykit.provably.ai/docs/cookbooks/langgraph_multi_agent) and [CrewAI](https://sourcerykit.provably.ai/docs/cookbooks/crewai_multi_agent) cookbooks hand `str(report)` to a healer agent and verify its corrected answer.

## Checking a Report

```python
await sourcerykit.check(report: Report, *, api_url: str = "https://api.provably.ai") -> list[str]
```

Returns what is wrong with the report, or `[]`. It needs no Provably account. It:

- sends every proof to Provably's public verifier, which confirms the proof and returns the query and the result it proves;
- confirms each proof is for the step's query, and that the step's answer is the proven result;
- confirms every claim is one the linker's reply holds and the guards accept;
- recomputes every verdict and the outcome from the proven results.

A changed value, verdict, check or proof is reported. What `check` cannot tell is whether the answer is complete, or whether the linker missed a statement: read the not verifiable, dropped and unchecked lines.

---

For how calls are recorded, see [interceptor](https://sourcerykit.provably.ai/docs/pillars/interceptor). For keeping calls to some endpoints out of verification, see [trusted-endpoints](https://sourcerykit.provably.ai/docs/pillars/trusted-endpoints).
