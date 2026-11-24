# OpenAI Agents SDK

This example verifies the answer of an agent built with the [OpenAI Agents SDK](https://github.com/openai/openai-agents-python). The agent looks up the weather in London on Open-Meteo and answers in plain text; SourceryKit checks every statement of that answer against what Open-Meteo actually returned.

## How It Works

1. **Recording.** `bootstrap_system()` installs the HTTP interceptor. The tool makes its Open-Meteo request inside `async_intercept_context`, so the request and the raw response are stored in your database.
2. **The run.** The agent runs inside `async with run() as weather_run:`, which collects every call recorded during it, including calls made by the tasks the agent starts.
3. **Verification.** `weather_run.verify(answer, model=...)` turns the answer into claims with one call to a model (the linker) at `SOURCERYKIT_LINKER_URL`: Claude's Messages API when that is `https://api.anthropic.com`, an OpenAI-compatible chat API otherwise. The recorded responses are sealed in your database's facts table, and every query a claim runs on them is proven by the qedb middleware. The agent itself needs no special output format: its plain answer is what gets checked. `print(report)` shows the outcome and each claim with its proven values.
4. **Checking a report.** `check(report)` returns what is wrong with a report, or nothing. It sends every proof to Provably's public verifier, confirms each proof is for the query and answer the report shows, and recomputes the verdicts and the outcome. It needs no Provably account, so whoever receives the report can run it.

Every call recorded in the run counts as evidence. If one of your tools calls a URL the model can influence, such as a generic "fetch this page" tool, list the endpoints you trust with `insert_trusted_endpoint` (or `sourcerykit endpoints add`): from then on, calls to any other endpoint are still recorded, but left out of verification and listed in the report as untrusted, so a page the model chose cannot back its own answer.

The linker only writes how to check a statement ("the temperature field equals 14, in °C"); the verdict comes from the proven data. Its claims are rejected when a number in the quoted statement is not the value being checked, when the tolerance is wider than half a unit of the stated value, or when the check could shift the recorded value. A statement the recorded data cannot check, such as "in London" or a UV index the response does not hold, is listed as not verifiable, and numbers that no claim covers are listed as unchecked: you read them in the report.

The report gives:

| Field | Meaning |
|---|---|
| `outcome` | `CAUGHT` if a statement is contradicted by the recorded data; `PASS` if every statement matches it; `PASS_WITH_NOTES` if nothing is contradicted but some statements have no data to check them; `ERROR` if no claim could be checked |
| `claims` | Each claim's quote and verdict, and for each step its SQL, the proven answer and the proof; statements the data cannot check are listed as not verifiable |
| `dropped` | Claims the linker wrote that were rejected, and why |
| `unchecked` | Numbers, times and dates in the answer that no claim checks |
| `untrusted` | Recorded calls left out of verification, when you have listed trusted endpoints and these are not among them |
| `extractor`, `reply` | The linker model and its reply, as received |

---

## Environment Configuration

Run the interactive setup wizard to configure your SourceryKit project:

```bash
sourcerykit init
```

> [!IMPORTANT]
> The wizard configures the SourceryKit project. Set your LLM-provider variables separately:

| Variable | Required | Description |
|---|---|---|
| `MODEL_URL` | **yes** | Base URL of an OpenAI-compatible provider (e.g., `https://openrouter.ai/api/v1`). |
| `MODEL_API_KEY` | **yes** | API key for the provider. |
| `MODEL_NAME` | **yes** | The agent's model (e.g., `openai/gpt-4o-mini`). |
| `SOURCERYKIT_LINKER_URL` | **yes** | Where the model that turns the answer into claims runs: `https://api.anthropic.com` for Claude directly (install `sourcerykit[anthropic]`), or the base URL of an OpenAI-compatible chat API; with OpenRouter, the same as `MODEL_URL`. |
| `SOURCERYKIT_LINKER_API_KEY` | **yes** | API key for it; with OpenRouter, the same as `MODEL_API_KEY`. For Claude, it may be left unset when `ANTHROPIC_API_KEY` is set. |
| `LINKER_MODEL` | no | The model that turns the answer into claims, e.g. `claude-haiku-5-5` on Claude's API or `anthropic/claude-haiku-5.5` on OpenRouter; defaults to `MODEL_NAME`. |

---

## Execution

1. Install the packages:
   ```bash
   pip install sourcerykit openai-agents python-dotenv httpx
   ```
2. Export the LLM-provider variables, or put them in a local `.env` file:
   ```bash
   export MODEL_URL="https://openrouter.ai/api/v1"
   export MODEL_API_KEY="sk-or-..."
   export MODEL_NAME="openai/gpt-4o-mini"
   export SOURCERYKIT_LINKER_URL="https://openrouter.ai/api/v1"
   export SOURCERYKIT_LINKER_API_KEY="sk-or-..."
   export LINKER_MODEL="anthropic/claude-haiku-5.5"
   ```
3. Run the example:
   ```bash
   # An honest answer: PASS, or PASS_WITH_NOTES when it names London, which the response does not
   python agent_run.py

   # Asked for the UV index and the chance of rain as well, which the response does not hold
   python agent_run.py --hallucinate
   ```

With `--hallucinate`, the agent is asked for values Open-Meteo did not return here and told to answer everything; nothing tells it to make anything up. Whether it invents them is up to the model: when it does, the invented values are listed as not verifiable; when it says they are not available, those statements are. With `openai/gpt-5-nano`, 5 of our 6 runs invented them.
