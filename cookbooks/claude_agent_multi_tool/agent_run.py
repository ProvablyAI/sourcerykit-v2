"""
Runnable demo: Claude Agent SDK + SourceryKit, one tool called several times.

The agent calls the same weather tool for London and for Paris, then compares them. The two
Open-Meteo responses have the same fields; SourceryKit checks each statement against the
response for the right city, and each step in the report reads one named call.

Run:
    python agent_run.py                # honest answer: PASS, or PASS_WITH_NOTES
    python agent_run.py --hallucinate  # asked for values the lookups lack: invented ones are listed
"""

import argparse
import asyncio
import json
import logging
import os
from typing import Any

import httpx
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server, query, tool
from dotenv import load_dotenv

from sourcerykit import async_intercept_context, bootstrap_system, check, run

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(name)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


_OPEN_METEO_BASE_URL = "https://api.open-meteo.com/v1/forecast"
_CITIES = {"London": (51.5074, -0.1278), "Paris": (48.8566, 2.3522)}
_MODEL = os.getenv("MODEL_NAME", "claude-haiku-5-5")
# The model that turns the answer into claims, at SOURCERYKIT_LINKER_URL.
_LINKER_MODEL = os.getenv("LINKER_MODEL", _MODEL)


@tool("get_current_weather", "Fetch the current temperature and wind speed in London or Paris", {"city": str})
async def get_current_weather(args: dict[str, Any]) -> dict[str, Any]:
    if args["city"] not in _CITIES:
        return {"content": [{"type": "text", "text": f"Unknown city; use one of {', '.join(_CITIES)}."}]}
    latitude, longitude = _CITIES[args["city"]]
    async with async_intercept_context(agent_id="demo", action_name="get_weather"):
        async with httpx.AsyncClient() as client:
            response = await client.get(
                _OPEN_METEO_BASE_URL,
                params={"latitude": latitude, "longitude": longitude, "current": "temperature_2m,wind_speed_10m"},
                timeout=30,
            )
            response.raise_for_status()
    return {"content": [{"type": "text", "text": json.dumps(response.json())}]}


async def main(hallucinate: bool = False) -> None:
    # 1. Initialize SourceryKit: schema, handshake, HTTP interceptor
    await bootstrap_system()

    # 2. Configure the MCP server with the weather tool, and the agent
    instructions = "You are a weather assistant. Call get_current_weather once for each city, then answer the user."
    prompt = "Compare the weather in London and Paris right now: which one is warmer, and by how much?"
    if hallucinate:
        # The lookups have no UV index or rain: pushed to answer everything, models tend to invent them
        instructions += " Always give a value for everything the user asks."
        prompt += " Also give the UV index and the chance of rain in each city."
    options = ClaudeAgentOptions(
        system_prompt=instructions,
        mcp_servers={"weather": create_sdk_mcp_server(name="weather", tools=[get_current_weather])},
        tools=[],  # no built-in tools: the agent answers from the weather tool alone
        allowed_tools=["mcp__weather__get_current_weather"],
        model=_MODEL,
    )

    # 3. Run the agent inside a SourceryKit run, which collects the calls its tools make
    print("Running agent...")
    answer = None
    async with run() as weather_run:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                answer = message.result
    if not answer:
        raise RuntimeError("The agent returned no answer.")
    print(f"\nAgent answer: {answer}")

    # 4. Verify the answer: the linker model turns it into claims, proven on the recorded data
    print("Verifying the answer...")
    report = await weather_run.verify(answer, model=_LINKER_MODEL)
    print(f"\n{report}")

    # 5. Check the report as whoever receives it would: its proofs, claims and verdicts
    problems = await check(report)
    print("\nReport check:", "every proof verifies" if not problems else "\n  ".join(["FAILED", *problems]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SourceryKit Claude Agent SDK multi-tool demo")
    parser.add_argument(
        "--hallucinate",
        action="store_true",
        help="Ask for values the lookups do not return, pushing the agent to invent them.",
    )
    args = parser.parse_args()
    asyncio.run(main(hallucinate=args.hallucinate))
