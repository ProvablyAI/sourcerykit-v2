# OpenAI Agents SDK: multi-agent customer support

This example verifies the reply of a multi-agent pipeline built with the [OpenAI Agents SDK](https://github.com/openai/openai-agents-python). An orchestrator answers a customer with the help of three specialists, each calling one endpoint of a support API; SourceryKit checks every statement of the reply the customer receives against what the API actually returned.

## Flow

```text
Customer: "I'm CUST-001. Where is my order ORD-123, what is the return policy for electronics, ..."
  async with run():                                        ← collects every recorded call below
    Orchestrator
      → order_status_specialist     → GET /orders/ORD-123        (recorded)
      → return_policy_specialist    → GET /policies/electronics  (recorded)
      → account_balance_specialist  → GET /accounts/CUST-001     (recorded)
      → writes the reply
  verify(reply) → each statement proven against the three responses
```

## How It Works

1. **The support API.** A small local server, started by the script on `http://127.0.0.1:47231`, serves three tables (orders, policies, accounts) as JSON, so the example runs with one command.
2. **Recording.** Each specialist's tool makes its request inside `async_intercept_context(agent_id=..., action_name=...)`, so every request and response is stored in your database.
3. **The run.** The whole pipeline runs inside one `async with run() as support_run:`, which collects the calls of every specialist, including those made inside the tasks the SDK starts.
4. **Verification.** `support_run.verify(reply, model=...)` checks the orchestrator's final reply, the one the customer receives: a value is right only if it matches the response it came from, whichever specialist fetched it. A specialist's mistake shows up when it reaches the reply.
5. **Checking a report.** `check(report)` verifies every proof with Provably's public verifier and recomputes the verdicts and the outcome; it needs no Provably account.

The specialists and the orchestrator answer in plain text: no output schema or extra code is needed to make them verifiable. The [OpenAI Agents SDK cookbook](../openai_agents/README.md) explains the report fields and the outcomes.

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
| `MODEL_URL` | **yes** | Base URL of an OpenAI-compatible provider (e.g., `https://openrouter.ai/api/v1`). |
| `MODEL_API_KEY` | **yes** | API key for the provider. |
| `MODEL_NAME` | **yes** | The agents' model (e.g., `openai/gpt-4o-mini`). |
| `SOURCERYKIT_LINKER_URL` | **yes** | Where the model that turns the reply into claims runs: `https://api.anthropic.com` for Claude directly (install `sourcerykit[anthropic]`), or the base URL of an OpenAI-compatible chat API; with OpenRouter, the same as `MODEL_URL`. |
| `SOURCERYKIT_LINKER_API_KEY` | **yes** | API key for it; with OpenRouter, the same as `MODEL_API_KEY`. |
| `LINKER_MODEL` | no | The linker model, e.g. `anthropic/claude-haiku-5.5` on OpenRouter; defaults to `MODEL_NAME`. |

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
   # An honest reply: PASS, or PASS_WITH_NOTES when it states something the responses cannot check
   python agent_run.py

   # The return policy specialist has outdated notes: 14 days, no restocking fee
   python agent_run.py --hallucinate
   ```

With `--hallucinate`, the return policy specialist's instructions carry outdated handbook notes that disagree with the API (30 days, 15% restocking fee); nothing tells it to prefer them. Agents often answer from what they "know" rather than from what their tools return. When it does, the reply is `CAUGHT`: the proofs show the API's 30 days and restocking fee. With `openai/gpt-5-nano`, all 3 of our runs trusted the notes.
