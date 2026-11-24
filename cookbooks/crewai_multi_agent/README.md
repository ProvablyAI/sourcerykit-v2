# CrewAI: an invoice status update from a crew of specialists

This example verifies the output of a [CrewAI](https://github.com/crewAIInc/crewAI) Flow and routes on the result. Three specialists each look up one part of an invoice on an ERP API, and an accounts payable manager writes the vendor a status update; SourceryKit checks every statement of the update against what the API returned; unless every statement is proven, a remediator rewrites it from the report, and the new update is verified again.

## Flow

```text
async with run():                                        ← collects every recorded call below
  @start write_update
    Crew (sequential):
      Amount Validator   → GET /invoices/INV-2026-8941    (recorded)
      Vendor Checker     → GET /vendors/INV-2026-8941     (recorded)
      Currency Verifier  → GET /currencies/INV-2026-8941  (recorded)
      Accounts Payable Manager → writes the vendor a status update
  @router verify → run.verify(update) ──PASS──→ done
                          │  ▲
              not PASS    ▼  │  (once)
  @listen remediate → rewrites the update from the report
```

## How It Works

1. **The ERP API.** A small local server, started by the script on `http://127.0.0.1:47231`, serves three tables (invoices, vendors, currencies) as JSON, so the example runs with one command.
2. **Recording.** Each specialist's tool, an `ERPLookup` for its table, makes its request inside `async_intercept_context`, so every request and response is stored in your database.
3. **The run.** The whole flow runs inside `async with run() as flow.invoice_run:`, which collects the calls of every specialist.
4. **Verification and routing.** The `verify` router checks the manager's update, the one the vendor receives, with `invoice_run.verify(update, model=...)`. On `PASS` the flow ends; otherwise the remediator gets the update and `str(report)`, replaces each contradicted statement with the proven value and leaves out what the data cannot check, and its update is verified again.
5. **Checking a report.** `check(report)` verifies every proof of the final report with Provably's public verifier and recomputes the verdicts and the outcome; it needs no Provably account.

The specialists and the manager answer in plain text: no output schema or extra code is needed to make them verifiable. The [OpenAI Agents SDK cookbook](../openai_agents/README.md) explains the report fields and the outcomes.

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
| `MODEL_NAME` | **yes** | The agents' model as CrewAI names it, e.g. `openrouter/openai/gpt-4o-mini` (install `crewai[litellm]`) or `openai/gpt-4o-mini`. |
| `OPENROUTER_API_KEY` | **yes** | Your provider's API key, under the name CrewAI expects for it (e.g. `OPENAI_API_KEY` for OpenAI). |
| `SOURCERYKIT_LINKER_URL` | **yes** | Where the model that turns the update into claims runs: `https://api.anthropic.com` for Claude directly (install `sourcerykit[anthropic]`), or the base URL of an OpenAI-compatible chat API such as `https://openrouter.ai/api/v1`. |
| `SOURCERYKIT_LINKER_API_KEY` | **yes** | API key for it; for Claude, it may be left unset when `ANTHROPIC_API_KEY` is set. |
| `LINKER_MODEL` | **yes** | The linker model, e.g. `anthropic/claude-haiku-5.5` on OpenRouter or `claude-haiku-5-5` on Claude's API. |

---

## Execution

1. Install the packages:
   ```bash
   pip install sourcerykit "crewai[litellm]" python-dotenv httpx
   ```
2. Export the LLM-provider variables, or put them in a local `.env` file:
   ```bash
   export MODEL_NAME="openrouter/openai/gpt-4o-mini"
   export OPENROUTER_API_KEY="sk-or-..."
   export SOURCERYKIT_LINKER_URL="https://openrouter.ai/api/v1"
   export SOURCERYKIT_LINKER_API_KEY="sk-or-..."
   export LINKER_MODEL="anthropic/claude-haiku-5.5"
   ```
3. Run the example:
   ```bash
   # An honest update: PASS, or remediated when it states something the responses cannot check
   python agent_run.py

   # The manager follows an outdated approval policy
   python agent_run.py --hallucinate
   ```

With `--hallucinate`, the manager's backstory carries a company policy that disagrees with the API: invoices over 10,000 USD stay pending until the CFO signs, and the CFO has signed nothing this month. The API says the invoice is `APPROVED`. Nothing tells the manager which source to prefer. When it applies the policy, the update is `CAUGHT`: the proofs show `APPROVED`, and the remediator rewrites the update from them. With `openai/gpt-5-nano`, all 3 of our runs applied the policy, and 2 of the 3 remediated updates were proven `PASS`; the third still had a statement the data cannot check.
