"""
Runnable demo: LangChain + SourceryKit, verifying the agent's answer.

The agent answers in plain text from an Open-Meteo weather lookup. SourceryKit records the
lookup, then checks every statement of the answer against what Open-Meteo actually returned,
with a proof for every value it relies on; anyone given the report can check those proofs.

Run:
    python agent_run.py                # honest answer: PASS, or PASS_WITH_NOTES
    python agent_run.py --hallucinate  # asked for values the lookup lacks: invented ones are listed
"""

import argparse
import asyncio
import logging
import os

import httpx
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.tools import tool

from sourcerykit import async_intercept_context, bootstrap_system, check, run

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(name)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


_OPEN_METEO_BASE_URL = "https://api.open-meteo.com/v1/forecast"
_MODEL = os.getenv("MODEL_NAME", "")
# The model that turns the answer into claims, at SOURCERYKIT_LINKER_URL.
_LINKER_MODEL = os.environ["LINKER_MODEL"]


@tool
async def get_current_weather_london() -> dict:
    """Fetch the current temperature, humidity and wind speed in London from Open-Meteo."""
    async with async_intercept_context(agent_id="demo", action_name="get_weather"):
        async with httpx.AsyncClient() as client:
            response = await client.get(
                _OPEN_METEO_BASE_URL,
                params={
                    "latitude": 51.5074,
                    "longitude": -0.1278,
                    "current": "temperature_2m,relative_humidity_2m,wind_speed_10m",
                },
                timeout=30,
            )
            response.raise_for_status()
            return response.json()


async def main(hallucinate: bool = False) -> None:
    # 1. Initialize SourceryKit: schema, handshake, HTTP interceptor
    await bootstrap_system()

    # 2. Create the agent
    instructions = "You are a weather assistant. Call the get_current_weather_london tool, then answer the user."
    prompt = "What is the weather in London right now?"
    if hallucinate:
        # The lookup has no UV index or rain: pushed to answer everything, models tend to invent them
        instructions += " Always give a value for everything the user asks."
        prompt = "What's the weather in London right now, and what's the UV index and the chance of rain?"
    agent = create_agent(model=_MODEL, tools=[get_current_weather_london], system_prompt=instructions)

    # 3. Run the agent inside a SourceryKit run, which collects the calls its tools make
    print("Running agent...")
    async with run() as weather_run:
        result = await agent.ainvoke({"messages": [{"role": "user", "content": prompt}]})
    answer = result["messages"][-1].text
    print(f"\nAgent answer: {answer}")

    # 4. Verify the answer: the linker model turns it into claims, proven on the recorded data
    print("Verifying the answer...")
    report = await weather_run.verify(answer, model=_LINKER_MODEL)
    print(f"\n{report}")

    # 5. Check the report as whoever receives it would: its proofs, claims and verdicts
    problems = await check(report)
    print("\nReport check:", "every proof verifies" if not problems else "\n  ".join(["FAILED", *problems]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SourceryKit LangChain demo")
    parser.add_argument(
        "--hallucinate",
        action="store_true",
        help="Ask for values the lookup does not return, pushing the agent to invent them.",
    )
    args = parser.parse_args()
    asyncio.run(main(hallucinate=args.hallucinate))
