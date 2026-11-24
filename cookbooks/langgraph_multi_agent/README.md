# LangGraph: a flight status pipeline that heals itself

This example verifies the answer of a [LangGraph](https://github.com/langchain-ai/langgraph) pipeline and routes on the result. A fetcher agent looks up a flight and answers the traveller; SourceryKit checks every statement against what the flight API returned; unless every statement is proven, a healer agent rewrites the answer from the report, and the new answer is verified again.

## Flow

```text
async with run():                                   ← collects every recorded call below
  START → fetcher → verifier ──PASS──────────────→ END
                      │  ▲
          not PASS    ▼  │  (once)
                     healer
```

1. **fetcher** calls `get_flight_status` (`GET /flights/BA2490`, recorded) and answers in plain text.
2. **verifier** runs `run.verify(answer, model=...)` and keeps the report in the graph state.
3. **healer**, when the outcome is not `PASS`: gets the answer and `str(report)`, replaces each contradicted statement with the proven value and leaves out what the data cannot check. Its answer goes back to the verifier; the graph heals once at most.

## How It Works

1. **The flight API.** A small local server, started by the script on `http://127.0.0.1:47231`, serves the flight as JSON, so the example runs with one command.
2. **Recording.** The tool makes its request inside `async_intercept_context`, so the request and the response are stored in your database.
3. **The run.** The whole graph runs inside `async with run() as flight_run:`; the `Run` travels in the graph state, so the verifier node can verify each answer against the calls recorded so far.
4. **Healing from the report.** The report shows each statement's verdict and the proven values, so the healer needs no access to the API: what it writes is verified like any other answer.
5. **Checking a report.** `check(report)` verifies every proof of the final report with Provably's public verifier and recomputes the verdicts and the outcome; it needs no Provably account.

The [OpenAI Agents SDK cookbook](../openai_agents/README.md) explains the report fields and the outcomes.

> [!NOTE]
> If your project lists trusted endpoints, add the local API to them, or its calls are left out of verification and listed as untrusted:
> `sourcerykit endpoints add http://127.0.0.1:47231`

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
| `MODEL_NAME` | **yes** | The agents' model as LangChain names it, e.g. `openrouter:openai/gpt-4o-mini` (install `langchain-openrouter`). |
| `OPENROUTER_API_KEY` | **yes** | Your provider's API key, under the name LangChain expects for it. |
| `SOURCERYKIT_LINKER_URL` | **yes** | Where the model that turns an answer into claims runs: `https://api.anthropic.com` for Claude directly (install `sourcerykit[anthropic]`), or the base URL of an OpenAI-compatible chat API such as `https://openrouter.ai/api/v1`. |
| `SOURCERYKIT_LINKER_API_KEY` | **yes** | API key for it; for Claude, it may be left unset when `ANTHROPIC_API_KEY` is set. |
| `LINKER_MODEL` | **yes** | The linker model, e.g. `anthropic/claude-haiku-5.5` on OpenRouter or `claude-haiku-5-5` on Claude's API. |

---

## Execution

1. Install the packages:
   ```bash
   pip install sourcerykit langchain langchain-openrouter langgraph python-dotenv httpx
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
   # An honest answer: PASS, or healed when it states something the response cannot check
   python agent_run.py

   # The fetcher applies a general fog rule to this flight
   python agent_run.py --hallucinate
   ```

With `--hallucinate`, the fetcher's instructions say Heathrow has fog today and that in fog, flights to Paris are delayed and board from gate A10; the API says this flight is on time at gate B42. Nothing tells the agent which source to prefer. When it applies the rule, the answer is `CAUGHT`: the proofs show `ON_TIME` and `B42`, and the healer rewrites the answer from them. With `openai/gpt-5-nano`, all 3 of our runs applied the rule, and all 3 healed answers were proven `PASS`.
