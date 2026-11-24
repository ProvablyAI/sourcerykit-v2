# End-to-End Walkthrough
This walkthrough follows one verifiable agent run with the SourceryKit SDK: record the calls a tool makes, run an agent, verify its plain-text answer, and check the report as someone who receives it would.

## Prerequisites
Run the interactive setup wizard to configure your project:

```bash
sourcerykit init
```

This provisions a hosted sandbox database by default. To use your own PostgreSQL instead, pass `--postgres-url` (must be hosted and publicly reachable — `localhost` will not work).

Then tell SourceryKit where the linker model runs, the model that turns an answer into checks; here, Claude Haiku on OpenRouter:

```bash
export SOURCERYKIT_LINKER_URL="https://openrouter.ai/api/v1"
export SOURCERYKIT_LINKER_API_KEY="sk-or-..."
```

For Claude's API, see [the linker](https://sourcerykit.provably.ai/docs/getting_started/onboarding#the-linker).

## Step-by-Step Implementation
### Step 1: Initialization

Bootstrap the runtime once, before any other SDK call. This prepares the database schema, completes the handshake with Provably, and patches the supported HTTP libraries (`httpx`, `aiohttp` and `requests`) process-wide.

```python
import httpx
from agents import Agent, Runner, function_tool

from sourcerykit import async_intercept_context, bootstrap_system, check, run

await bootstrap_system()
```

### Step 2: A Recorded Tool
Wrap the tool's HTTP call in `async_intercept_context`: the request and the exact response text are stored in your database, tagged with `agent_id` and `action_name`. The tool returns the plain response.

```python
@function_tool
async def get_current_weather_london() -> dict:
    """Fetch the current temperature, humidity and wind speed in London from Open-Meteo."""
    async with async_intercept_context(agent_id="demo", action_name="get_weather"):
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": 51.5074,
                    "longitude": -0.1278,
                    "current": "temperature_2m,relative_humidity_2m,wind_speed_10m",
                },
                timeout=30,
            )
            response.raise_for_status()
            return response.json()
```

> [!TIP]
> The same tool can be called many times in one run, e.g. for London and Paris: verification tells the calls apart by their URL and content. See the [claude_agent_multi_tool](https://sourcerykit.provably.ai/docs/cookbooks/claude_agent_multi_tool) cookbook.

### Step 3: Run the Agent
Run the agent inside `run()`, which collects every call recorded in the block, including those made by the tasks the agent starts. The agent answers in plain text: no output schema is needed.

```python
agent = Agent(
    name="weather-demo",
    instructions="You are a weather assistant. Call the get_current_weather_london tool, then answer the user.",
    tools=[get_current_weather_london],
    model=MODEL_NAME,
)
async with run() as weather_run:
    result = await Runner.run(agent, "What is the weather in London right now?")
```

### Step 4: Verify the Answer
`verify` sends the answer and the recorded response to the linker, which writes how to check each statement; the response is sealed in your `facts` table, and every query the checks run on it is proven.

```python
report = await weather_run.verify(result.final_output, model="anthropic/claude-haiku-5.5")
print(report)
```

### Step 5: Check the Report
Whoever receives the report can check it, with no Provably account: every proof goes to Provably's public verifier, and every verdict is recomputed from what the proofs show.

```python
problems = await check(report)
print("every proof verifies" if not problems else problems)
```

## Reading the Outcomes
The [openai_agents](https://sourcerykit.provably.ai/docs/cookbooks/openai_agents) cookbook is these steps as one script, `python agent_run.py`.

### An Honest Answer (`PASS_WITH_NOTES`)

```text
Agent answer: Here’s the current weather in London (GMT):

- Temperature: 5.7°C
- Humidity: 77%
- Wind: 14 km/h

Time: 17:30 GMT on 2026-11-24

It’s fairly cool and a bit humid. You might want a light jacket if you’re outdoors.

Outcome: PASS_WITH_NOTES   (claims by linker:anthropic/claude-haiku-5.5)
  SUPPORTED      'Temperature: 5.7°C'  proven: v=5.7000  u=°C
  SUPPORTED      'Humidity: 77%'  proven: v=77  u=%
  SUPPORTED      'Wind: 14 km/h'  proven: v=14  u=km/h
  SUPPORTED      'Time: 17:30 GMT on 2026-11-24'  proven: t=2026-11-24T17:30  z=GMT
  NOT VERIFIABLE 'current weather in London'  the responses give only coordinates and the timezone GMT; no field names London as the location
  SUPPORTED      '(GMT)'  proven: z=GMT

Report check: every proof verifies
```

Every value is backed by a proven query on the recorded response. The response gives coordinates, not a place name, so "in London" is not verifiable: nothing is wrong, but you read that statement yourself. When every statement is checked and backed, the outcome is `PASS`. The advice about a jacket is not a factual statement about the data, so no claim covers it.

### Values the Data Lacks (`PASS_WITH_NOTES`)
`python agent_run.py --hallucinate` asks for the UV index and the chance of rain as well, which this response does not hold. An agent that invents them gets each invented value listed as not verifiable; this one said they were not available:

```text
Outcome: PASS_WITH_NOTES   (claims by linker:anthropic/claude-haiku-5.5)
  SUPPORTED      '5.7°C'  proven: t=5.7000  u=°C
  ...
  NOT VERIFIABLE 'UV index: not available in this data feed'  the response has no UV index field
  NOT VERIFIABLE 'Chance of rain: not available in this data feed'  the response has no precipitation probability field
  UNCHECKED      '17:30': no claim checks it
```

`UNCHECKED` lists numbers, times and dates in the answer that no claim checks: here, a time the linker did not cover.

### A Contradicted Answer (`CAUGHT`)
In the [openai_agents_multi_agent](https://sourcerykit.provably.ai/docs/cookbooks/openai_agents_multi_agent) cookbook, `--hallucinate` gives the return policy specialist outdated handbook notes: 14 days, no restocking fee. The support API says 30 days, with a 15% restocking fee. When the agent repeats its notes:

```text
Outcome: CAUGHT   (claims by linker:anthropic/claude-haiku-5.5)
  CONTRADICTED   'Returns are accepted within 14 days of purchase'  proven: d=30
  CONTRADICTED   'No restocking fee'  proven: f=1
```

Each contradicted statement shows the proven value it was checked against. To act on a `CAUGHT`, route on `report.outcome`, as the [LangGraph](https://sourcerykit.provably.ai/docs/cookbooks/langgraph_multi_agent) and [CrewAI](https://sourcerykit.provably.ai/docs/cookbooks/crewai_multi_agent) cookbooks do.

---

**Next steps:**

- [Architecture](https://sourcerykit.provably.ai/docs/pillars/architecture) — what each piece does and how they fit together
- [Verification](https://sourcerykit.provably.ai/docs/pillars/verification) — the linker, proofs, the report and `check` in depth
- [Onboarding](https://sourcerykit.provably.ai/docs/getting_started/onboarding) — first-time account, credentials, and database setup
