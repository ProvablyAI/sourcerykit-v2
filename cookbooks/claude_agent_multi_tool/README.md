# Claude Agent SDK: one tool, several calls

This example verifies an answer built from several calls to the **same tool**. An agent built with the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) calls `get_current_weather` once for London and once for Paris, then compares them. Both Open-Meteo responses have the same fields, so a value is only right if it comes from the response for the right city.

## How It Works

1. **Recording.** Each tool call makes its Open-Meteo request inside `async_intercept_context`, so both requests and responses are stored in your database. The tool returns the plain response: no reference or extra field is needed.
2. **The run.** `query()` runs inside `async with run() as weather_run:`, which collects both calls.
3. **Verification.** `weather_run.verify(answer, model=...)` sends the answer and every recorded call, with its URL, to the linker model. The URL's coordinates tell the calls apart, so each claim names the call it reads, for example London's temperature from the London call. A comparison such as "Paris is warmer, by 1.1 °C" reads both calls and is checked publicly from the two proven values.
4. **Checking a report.** `check(report)` verifies every proof with Provably's public verifier and recomputes the verdicts and the outcome; it needs no Provably account.

```text
get_current_weather(London) → call_1: {"latitude": 51.5, ..., "current": {"temperature_2m": 6.5, ...}}
get_current_weather(Paris)  → call_2: {"latitude": 48.9, ..., "current": {"temperature_2m": 7.6, ...}}

Answer: "Paris is warmer, by 1.1 °C. London: 6.5 °C ..."
  SUPPORTED 'Paris is warmer, by 1.1 °C'  proven: paris=7.6 (call_2)  london=6.5 (call_1)
  SUPPORTED '6.5 °C'                      proven: london=6.5 (call_1)
```

If the agent swapped the two cities' values, the claims would read each value from its city's call and the answer would be `CAUGHT`. The [OpenAI Agents SDK cookbook](../openai_agents/README.md) explains the report fields, the outcomes and when to list trusted endpoints.

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
| `ANTHROPIC_API_KEY` | no | API key for Claude; the Claude Agent SDK can also use your Claude Code login. |
| `MODEL_NAME` | no | The agent's model; defaults to `claude-haiku-5-5`. |
| `SOURCERYKIT_LINKER_URL` | **yes** | Where the model that turns the answer into claims runs: `https://api.anthropic.com` for Claude directly, or the base URL of an OpenAI-compatible chat API such as `https://openrouter.ai/api/v1`. |
| `SOURCERYKIT_LINKER_API_KEY` | no | API key for it; for Claude, it may be left unset when `ANTHROPIC_API_KEY` is set. |
| `LINKER_MODEL` | no | The linker model, e.g. `claude-haiku-5-5` on Claude's API or `anthropic/claude-haiku-5.5` on OpenRouter; defaults to `MODEL_NAME`. |

---

## Execution

1. Install the packages:
   ```bash
   pip install "sourcerykit[anthropic]" claude-agent-sdk python-dotenv httpx
   ```
2. Export the LLM-provider variables, or put them in a local `.env` file:
   ```bash
   export ANTHROPIC_API_KEY="sk-ant-..."
   export SOURCERYKIT_LINKER_URL="https://api.anthropic.com"
   ```
3. Run the example:
   ```bash
   # An honest comparison: PASS, or PASS_WITH_NOTES when it states something the responses cannot check
   python agent_run.py

   # Asked for the UV index and the chance of rain as well, which the responses do not hold
   python agent_run.py --hallucinate
   ```

With `--hallucinate`, the agent is told to answer everything; nothing tells it to make anything up. Whether it invents the missing values is up to the model: when it does, they are listed as not verifiable. `claude-haiku-5-5` usually says they are not available.
