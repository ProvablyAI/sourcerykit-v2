<div align="center">
  <img src="https://raw.githubusercontent.com/ProvablyAI/sourcerykit/refs/heads/main/docs/images/logo.svg" alt="SourceryKit" width="220" />

  <br />

  [![PyPI version](https://img.shields.io/pypi/v/sourcerykit)](https://pypi.org/project/sourcerykit/)
  [![Python versions](https://img.shields.io/pypi/pyversions/sourcerykit)](https://pypi.org/project/sourcerykit/)
  [![License: BSL 1.1](https://img.shields.io/badge/license-BSL%201.1-blue)](https://github.com/ProvablyAI/sourcerykit/blob/main/LICENSE.md)
  [![CI](https://github.com/ProvablyAI/sourcerykit/actions/workflows/ci.yml/badge.svg)](https://github.com/ProvablyAI/sourcerykit/actions/workflows/ci.yml)
</div>


SourceryKit is the Python SDK for [Provably](https://provably.ai). It makes agent answers verifiable: it records the HTTP calls an agent's tools make, then checks every statement of the agent's plain-text answer against what those calls actually returned. Every value a verdict relies on comes with a proof that anyone can check: your team, your auditor, or the agent you hand off to. A statement the data contradicts is `CAUGHT` before it ships ([see our benchmark](https://provably.ai/blogs/The-Agent-Was-Right-The-Evidence-Wasnt)).

SourceryKit is powered by Provably's [QEDB verifiable database](https://eprint.iacr.org/2025/1408), accepted at [ACM CCS 2026](https://www.sigsac.org/ccs/CCS2026/). QEDB proves that SQL query results were computed correctly over the complete committed data, producing small, database size independent proofs averaging around 1 KB that can be proven and verified in milliseconds.

> ⚠️ **IMPORTANT:** Upgrading from a previous version? See the [Migration Guides](https://github.com/ProvablyAI/sourcerykit/blob/main/docs/migrations/README.md).

## Features

- **Verifies plain answers** — no output schema, no claims to write: pass the agent's final text answer, and each statement in it is checked against the recorded calls.
- **Proven verdicts** — the recorded responses are sealed in a QEDB-backed table, and every value a verdict relies on comes from a proven query. `check(report)` verifies a report without a Provably account.
- **Automatic HTTP interception** — records the requests and responses of `httpx`, `aiohttp` and `requests` with no changes to your tools' HTTP code.
- **Four outcomes** — `PASS`, `PASS_WITH_NOTES` (nothing contradicted, some statements not checkable), `CAUGHT` (a statement contradicted), `ERROR` (nothing verified).
- **Optional trusted endpoints** — list the endpoints you trust, and calls to any other are left out of verification and listed in the report.
- **Framework-agnostic** — drops into the OpenAI Agents SDK, LangChain, the Claude Agent SDK, CrewAI and LangGraph.

Under the hood, these are the pieces doing the work:

- **HTTP Interceptor**: Patches your HTTP libraries to record the calls made inside `async_intercept_context`.
- **Run**: `async with run()` collects the calls recorded inside it, including those of the tasks it starts.
- **Linker**: A model you choose reads the answer and the recorded responses, and writes how to check each statement. It only writes the checks: code guards reject a check that could pass a value the statement does not state, and the verdict comes from the data.
- **Facts Table**: The run's responses, flattened into rows and sealed by the QEDB middleware before any query runs.
- **Proofs**: Every query a check runs on the facts is proven; the report carries the proofs, and Provably's public verifier checks them.
- **Report**: The outcome, each statement's verdict and proven values, and what could not be checked.


## Quickstart

Requires **Python 3.12+**.

```bash
pip install sourcerykit
sourcerykit init          # one-time setup: account, sandbox, credentials
```

Prefer installing from source?

```bash
git clone git@github.com:ProvablyAI/sourcerykit.git
pip install -e ./sourcerykit
```

Record the tool's HTTP call inside an intercept context, run the agent inside `run()`, then verify its answer before you trust it. This example uses the OpenAI Agents SDK (`pip install openai-agents`), the free [Open-Meteo](https://open-meteo.com) API, and Claude Haiku as the linker on OpenRouter: set `SOURCERYKIT_LINKER_URL=https://openrouter.ai/api/v1` and `SOURCERYKIT_LINKER_API_KEY` to your OpenRouter key, or see [Configuration](#configuration) for Claude's API.

```python
import asyncio

import httpx
import sourcerykit
from agents import Agent, Runner, function_tool

URL = "https://api.open-meteo.com/v1/forecast"


# 1. Record the tool's HTTP call
@function_tool
async def get_london_temperature() -> dict:
    """Fetch the current temperature in London."""
    async with sourcerykit.async_intercept_context(agent_id="demo", action_name="get_weather"):
        async with httpx.AsyncClient() as client:
            response = await client.get(
                URL, params={"latitude": 51.5074, "longitude": -0.1278, "current": "temperature_2m"}
            )
            response.raise_for_status()
            return response.json()


async def main():
    # 2. Fire up the system: schema, handshake, HTTP interceptor
    await sourcerykit.bootstrap_system()

    # 3. Run the agent inside a run, which collects the calls its tools make
    agent = Agent(
        name="weather",
        instructions="Call get_london_temperature, then answer the user.",
        tools=[get_london_temperature],
    )
    async with sourcerykit.run() as weather_run:
        result = await Runner.run(agent, "What is the current temperature in London?")

    # 4. Verify the agent's answer against the recorded call, and print the report
    report = await weather_run.verify(result.final_output, model="anthropic/claude-haiku-5.5")
    print(report)  # PASS, PASS_WITH_NOTES, CAUGHT or ERROR, with each statement's proven values

    # 5. Anyone given the report can check its proofs, claims and verdicts
    print(await sourcerykit.check(report) or "every proof verifies")


asyncio.run(main())
```

For a runnable version with a `--hallucinate` flag that pushes the agent to invent values, see [cookbooks/openai_agents](https://github.com/ProvablyAI/sourcerykit/tree/main/cookbooks/openai_agents).


## Configuration
To get things running, SourceryKit must be configured with your project variables. The interactive CLI handles account provisioning, organization workspace initialization, database validation, and persists credentials globally (OS application folder) and locally (project `.env`).

```bash
sourcerykit init
```

The wizard will guide you through:
- **Account Login (OAuth)**: Log in securely with your browser and select your organization workspace.
- **API Key Generation**: Automatically fetch your SDK API-KEY from your account profile.
- **Database Provisioning**: Creates a hosted sandbox database automatically. To use your own PostgreSQL instead, pass `--postgres-url`.
- **Save Config**: Automatically write your credentials and tokens straight to a local .env file.

> ⚠️ **IMPORTANT:** The wizard only configures **SOURCERYKIT_*** variables. It does **not** handle third-party LLM provider infrastructure keys, which must still be exported separately.

### The linker

`verify` asks a model of your choice, the linker, to turn the answer into checks. Tell SourceryKit where it runs:

```bash
# Claude's API (pip install "sourcerykit[anthropic]"); the key may come from ANTHROPIC_API_KEY instead
export SOURCERYKIT_LINKER_URL="https://api.anthropic.com"
export SOURCERYKIT_LINKER_API_KEY="sk-ant-..."

# or any OpenAI-compatible chat API, such as OpenRouter
export SOURCERYKIT_LINKER_URL="https://openrouter.ai/api/v1"
export SOURCERYKIT_LINKER_API_KEY="sk-or-..."
```

Then pass the model to `verify(answer, model=...)`, e.g. `claude-haiku-5-5` on Claude's API or `anthropic/claude-haiku-5.5` on OpenRouter.

### Manual configuration (fallback)

Already have credentials, or need to bypass the wizard (CI, containers, debugging)? Environment
variables override the stored config:

```bash
export PROVABLY_ACCESS_TOKEN="..."
export PROVABLY_REFRESH_TOKEN="..."
export SOURCERYKIT_ORG_ID="..."
export SOURCERYKIT_POSTGRES_URL="postgresql://user:password@host:5432/db"
```

> [!NOTE]
> `SOURCERYKIT_POSTGRES_URL` is set automatically when using a sandbox. Only set it manually if you're using your own database.

For a full list of CLI commands, check out the [CLI Documentation](https://sourcerykit.provably.ai/docs/getting_started/cli) file, or simply run:
```bash
sourcerykit --help
```

For a full list of environment variables, see [.env.example](https://github.com/ProvablyAI/sourcerykit/blob/main/.env.example).


## More Docs
Want to dig into the details? Check out our documentation and specific guides:

* **Official Documentation:** Visit [provably.ai/docs](https://sourcerykit.provably.ai/docs) for the complete reference.
* [End-to-End Walkthrough](https://sourcerykit.provably.ai/docs/getting_started/end-to-end-walkthrough) — Get up and running from scratch.
* [Cookbooks & Examples](https://github.com/ProvablyAI/sourcerykit/tree/main/cookbooks) — Practical recipes and code samples.

### Technical Guides
- [Architecture Overview](https://sourcerykit.provably.ai/docs/pillars/architecture)
- [HTTP Interception](https://sourcerykit.provably.ai/docs/pillars/interceptor)
- [Managing Trusted Endpoints](https://sourcerykit.provably.ai/docs/pillars/trusted-endpoints)
- [Verification, Reports & Proofs](https://sourcerykit.provably.ai/docs/pillars/verification)


## Contributing
We welcome fixes, features, and doc updates! Check out [CONTRIBUTING.md](https://github.com/ProvablyAI/sourcerykit/blob/main/CONTRIBUTING.md) to see how to run tests and open up a pull request.

## License

This project is licensed under the [Business Source License 1.1](https://github.com/ProvablyAI/sourcerykit/blob/main/LICENSE.md).

- Copyright © 2026 Provably Technologies LTD
- You may not offer the Software as a commercial hosted service without purchasing a commercial license from [Provably Technologies Ltd](https://provably.ai).
- On 2029-05-07, the license will automatically convert to GPL-3.0-or-later.

See the [LICENSE](https://github.com/ProvablyAI/sourcerykit/blob/main/LICENSE.md) file for full terms and details.
