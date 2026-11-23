# OpenAI Agents SDK

This example verifies the answer of an agent built with the [OpenAI Agents SDK](https://github.com/openai/openai-agents-python). The agent looks up the weather in London on Open-Meteo and answers in plain text; SourceryKit checks every statement of that answer against what Open-Meteo actually returned.

## How It Works

1. **Recording.** `bootstrap_system()` installs the HTTP interceptor. The tool makes its Open-Meteo request inside `async_intercept_context`, so the request and the raw response are stored in your database. The endpoint is allow-listed first with `insert_trusted_endpoint`: a call to an endpoint that is not on the list is refused.
2. **The run.** The agent runs inside `async with run() as weather_run:`, which collects every call recorded during it, including calls made by the tasks the agent starts.
3. **Verification.** `weather_run.verify(answer, complete=..., model=...)` turns the answer into claims with one call to a model (the linker), then runs each claim's queries on the recorded responses. The agent itself needs no special output format: its plain answer is what gets checked.

The linker only writes how to check a statement ("the temperature field equals 14, in °C"); the verdict comes from the recorded data. Its claims are rejected when a number in the quoted statement is not the value being checked, when the tolerance is wider than half a unit of the stated value, or when the check could shift the recorded value. A number in the answer that no claim covers is listed as unchecked and makes the outcome `CAUGHT`, as does a number the recorded data does not hold: catching invented values is the point. A statement of words only that the data cannot check, such as "in London", is listed as not verifiable and leaves the answer passing, with notes for you to read.

The report gives:

| Field | Meaning |
|---|---|
| `outcome` | `PASS` if every statement matches the recorded data; `PASS_WITH_NOTES` if nothing is wrong but some statements (words, never numbers) have no data to check them, such as "in London"; `CAUGHT` if a statement is contradicted, or a number, time or date has no data behind it or is not checked; `ERROR` if no claim could be checked |
| `claims` | Each claim's quote, verdict, and the SQL it ran with the recorded values; statements the data cannot check are listed as not verifiable |
| `dropped` | Claims the linker wrote that were rejected, and why |
| `unchecked` | Numbers, times and dates in the answer that no claim checks |
| `extractor`, `reply` | The linker model and its reply, as received |

---

## Environment Configuration

Run the interactive setup wizard to configure your SourceryKit project:

```bash
sourcerykit init
```

> [!IMPORTANT]
> The wizard only configures **SOURCERYKIT_*** variables. Set your LLM-provider variables separately:

| Variable | Required | Description |
|---|---|---|
| `MODEL_URL` | **yes** | Base URL of an OpenAI-compatible provider (e.g., `https://openrouter.ai/api/v1`). |
| `MODEL_API_KEY` | **yes** | API key for the provider. |
| `MODEL_NAME` | **yes** | The agent's model (e.g., `openai/gpt-4o-mini`). |
| `LINKER_MODEL` | no | The model that turns the answer into claims; defaults to `MODEL_NAME`. |

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
   export LINKER_MODEL="anthropic/claude-haiku-5.5"
   ```
3. Run the example:
   ```bash
   # An honest answer: PASS
   python agent_run.py

   # The agent is told to change the temperature: CAUGHT
   python agent_run.py --tamper
   ```
