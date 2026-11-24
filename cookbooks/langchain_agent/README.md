# LangChain Agent

This example verifies the answer of an agent built with [LangChain](https://github.com/langchain-ai/langchain)'s `create_agent`. The agent looks up the weather in London on Open-Meteo and answers in plain text; SourceryKit checks every statement of that answer against what Open-Meteo actually returned.

## How It Works

1. **Recording.** `bootstrap_system()` installs the HTTP interceptor. The tool makes its Open-Meteo request inside `async_intercept_context`, so the request and the raw response are stored in your database.
2. **The run.** `agent.ainvoke(...)` runs inside `async with run() as weather_run:`, which collects every call recorded during it. The agent's answer is the text of its last message, `result["messages"][-1].text`: no `response_format` is needed.
3. **Verification.** `weather_run.verify(answer, model=...)` turns the answer into claims with one call to the linker model at `SOURCERYKIT_LINKER_URL`, proves each claim's queries on the recorded data, and returns a report; `print(report)` shows it.
4. **Checking a report.** `check(report)` verifies every proof with Provably's public verifier and recomputes the verdicts and the outcome; it needs no Provably account.

The [OpenAI Agents SDK cookbook](../openai_agents/README.md) explains the report fields, the outcomes and when to list trusted endpoints.

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
| `MODEL_NAME` | **yes** | The agent's model as LangChain names it, e.g. `openrouter:openai/gpt-4o-mini` (install `langchain-openrouter`). |
| `OPENROUTER_API_KEY` | **yes** | Your provider's API key, under the name LangChain expects for it. |
| `SOURCERYKIT_LINKER_URL` | **yes** | Where the model that turns the answer into claims runs: `https://api.anthropic.com` for Claude directly (install `sourcerykit[anthropic]`), or the base URL of an OpenAI-compatible chat API such as `https://openrouter.ai/api/v1`. |
| `SOURCERYKIT_LINKER_API_KEY` | **yes** | API key for it; for Claude, it may be left unset when `ANTHROPIC_API_KEY` is set. |
| `LINKER_MODEL` | **yes** | The linker model, e.g. `anthropic/claude-haiku-5.5` on OpenRouter or `claude-haiku-5-5` on Claude's API. |

---

## Execution

1. Install the packages:
   ```bash
   pip install sourcerykit langchain langchain-openrouter python-dotenv httpx
   ```
2. Export the LLM-provider variables, or put them in a local `.env` file:
   ```bash
   export MODEL_NAME="openrouter:openai/gpt-4o-mini"
   export OPENROUTER_API_KEY="sk-or-..."
   export SOURCERYKIT_LINKER_URL="https://openrouter.ai/api/v1"
   export SOURCERYKIT_LINKER_API_KEY="sk-or-..."
   export LINKER_MODEL="anthropic/claude-haiku-5.5"
   ```
3. Run the example:
   ```bash
   # An honest answer: PASS, or PASS_WITH_NOTES when it states something the response cannot check
   python agent_run.py

   # Asked for the UV index and the chance of rain as well, which the response does not hold
   python agent_run.py --hallucinate
   ```

With `--hallucinate`, the agent is asked for values Open-Meteo did not return here and told to answer everything; nothing tells it to make anything up. Whether it invents them is up to the model: when it does, the invented values are listed as not verifiable; when it says they are not available, those statements are.
