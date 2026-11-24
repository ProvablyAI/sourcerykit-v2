# AGENTS.md

> This file is the canonical agent entry point. It lives in the repository at
> https://github.com/ProvablyAI/sourcerykit/blob/main/AGENTS.md and is also mirrored as the
> machine view of https://provably.ai. If you reached it through the landing page, the repo
> copy is the source of truth — read it there when in doubt.

SourceryKit is the Python SDK for [Provably](https://provably.ai): it makes agent answers
verifiable. It records the HTTP calls an agent's tools make, then checks every statement of
the agent's plain-text answer against what those calls actually returned, with a proof for
every value it relies on that anyone can verify. A statement the data contradicts is
**caught** instead of shipped.

> [!IMPORTANT]
> **Setup is scriptable — prefer the `sourcerykit init` CLI over hand-writing config.**
> Exactly one step needs a human: completing the browser OAuth login (new accounts are
> created on the Provably web app during that login). Full steps:
> [onboarding.md](docs/getting_started/onboarding.md).

## Where to go next

| Your situation | Do this |
|---|---|
| First time — no Provably credentials yet | Run the `sourcerykit init` setup (see the note above) → [onboarding.md](docs/getting_started/onboarding.md). |
| Integrating SourceryKit into an agent | Open the closest cookbook ([below](#cookbooks-runnable-examples)), mirror it, then swap in your own tools. |
| Want to see one full run first | [end-to-end-walkthrough.md](docs/getting_started/end-to-end-walkthrough.md). |
| Got an unexpected `CAUGHT`, `PASS_WITH_NOTES` or `ERROR` | Read [the outcomes below](#the-flow-at-a-glance), then [verification.md](docs/pillars/verification.md). |
| Need a signature, type, or CLI flag | [public API](src/sourcerykit/__init__.py) · [verification.md](docs/pillars/verification.md) · [cli.md](docs/getting_started/cli.md) |
| Record or inspect outbound HTTP calls | [interceptor.md](docs/pillars/interceptor.md) |
| Keep calls to some endpoints out of verification | [trusted-endpoints.md](docs/pillars/trusted-endpoints.md) |
| Understand how the pieces fit | [architecture.md](docs/pillars/architecture.md) |
| Upgrade from an earlier version | [migration guides](docs/migrations/README.md) |

**Cookbooks are the ground truth — mirror one.** Everything else is supporting docs; load
only what a task needs.

## Cookbooks (runnable examples)

Each is a full runnable agent: `python agent_run.py` for an honest answer (`PASS`, or
`PASS_WITH_NOTES` when it states something the data cannot check); add `--hallucinate` to give
the agent a realistic reason to state something the data does not back, and watch the report
list or catch it. Whether the agent takes the bait is up to the model: each README gives the
rate we measured.

**Framework not listed?** The flow is identical everywhere — only how you read the agent's
final text answer is framework-specific. Copy the closest cookbook and change just that line.

**Verify the agent's own answer.** Run a real agent and pass its final text answer, as the
user would receive it, to `verify`. Do NOT write the answer or the claims yourself from the
fetched data — that skips the agent the whole system exists to verify. Using an
OpenAI-compatible endpoint (e.g. OpenRouter)? Point the framework's client at your
`MODEL_URL` with `MODEL_API_KEY`, like [cookbooks/openai_agents](cookbooks/openai_agents)
does — don't drop to bare HTTP.

**Single-agent** — one agent fetches and answers (weather):

| Cookbook | Framework | The answer comes from |
|---|---|---|
| [openai_agents](cookbooks/openai_agents) | OpenAI Agents SDK | `result.final_output`; its README explains the report |
| [claude_agent](cookbooks/claude_agent) | Claude Agent SDK | the `result` of the final `ResultMessage` |
| [langchain_agent](cookbooks/langchain_agent) | LangChain `create_agent` | `result["messages"][-1].text` |
| [claude_agent_multi_tool](cookbooks/claude_agent_multi_tool) | Claude Agent SDK | one tool called for two cities; each value proven from its own call |

**Multi-agent** — one `run()` around the whole pipeline; the final answer is verified:

| Cookbook | Framework | Pattern |
|---|---|---|
| [openai_agents_multi_agent](cookbooks/openai_agents_multi_agent) | OpenAI Agents SDK | Orchestrator → specialists → verify (customer support) |
| [langgraph_multi_agent](cookbooks/langgraph_multi_agent) | LangGraph | Fetcher → verifier → healer → verifier (flight status) |
| [crewai_multi_agent](cookbooks/crewai_multi_agent) | CrewAI | Crew → verify → remediate → verify (invoice status) |

## The flow at a glance

```python
await bootstrap_system()                              # init: schema, handshake, HTTP interceptor — first, once

async with async_intercept_context(agent_id=..., action_name=...):
    ...                                               # inside each tool: its HTTP call is recorded

async with run() as r:                                # collects every call recorded inside, also in tasks it starts
    answer = ...                                      # run the agent; take its final text answer

report = await r.verify(answer, model=LINKER_MODEL)   # the answer's statements, checked and proven
print(report)                                         # outcome, each statement's verdict and proven values
problems = await check(report)                        # [] when every proof, claim and verdict holds
```

`verify` sends the answer and the recorded responses to the linker model
(`SOURCERYKIT_LINKER_URL`, `SOURCERYKIT_LINKER_API_KEY`), which writes how to check each
statement; the verdict comes from the recorded data, sealed in your database, with a proof for
every query. `check(report)` needs no Provably account: hand the report to anyone.

Outcomes:

- `PASS` — every statement is checked and backed by the recorded data.
- `PASS_WITH_NOTES` — nothing is contradicted, but some statements have no data to check them;
  the report lists them as not verifiable.
- `CAUGHT` — a statement is contradicted by the recorded data.
- `ERROR` — no statement could be checked: nothing was verified. Verifying nothing is always
  `ERROR`, never a pass.

Async throughout — `await` every SDK call. Recorded traffic: `httpx`, `aiohttp`, and
`requests`. Every recorded call counts as evidence unless you list trusted endpoints; then
calls to other endpoints are left out and listed in the report as untrusted.
