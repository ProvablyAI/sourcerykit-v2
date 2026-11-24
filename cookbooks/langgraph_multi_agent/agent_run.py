"""
Runnable demo: LangGraph multi-agent + SourceryKit, a flight status pipeline that heals itself.

A fetcher agent looks up a flight on a flight status API and answers the traveller. SourceryKit
verifies the answer against what the API returned; unless every statement is proven, a healer
agent rewrites the answer from the report's proven values, and the new answer is verified again.

The flight API is a small local server started by this script, so it runs with one command.

Run:
    python agent_run.py                # an honest answer: PASS, or healed from PASS_WITH_NOTES
    python agent_run.py --hallucinate  # the fetcher applies a fog rule: CAUGHT, then healed
"""

import argparse
import asyncio
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal, TypedDict

import httpx
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph

from sourcerykit import Outcome, Report, Run, async_intercept_context, bootstrap_system, check, run

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(name)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

_MODEL = os.getenv("MODEL_NAME", "")
# The model that turns an answer into claims, at SOURCERYKIT_LINKER_URL.
_LINKER_MODEL = os.environ["LINKER_MODEL"]
# The local flight API; list it with `sourcerykit endpoints add` if your project lists trusted endpoints.
_API_URL = "http://127.0.0.1:47231"


# --- The flight status API: one local server ---
_FLIGHTS: dict[str, dict[str, Any]] = {
    "BA2490": {
        "flight": "BA2490",
        "route": "LHR→CDG",
        "status": "ON_TIME",
        "departure_time": "2026-07-09T14:30:00Z",
        "gate": "B42",
    },
}


class _FlightAPI(BaseHTTPRequestHandler):
    """GET /flights/<number>: the flight as JSON, or 404."""

    def do_GET(self) -> None:
        flight = _FLIGHTS.get(self.path.removeprefix("/flights/"))
        body = json.dumps(flight or {"error": "unknown flight"}).encode()
        self.send_response(200 if flight else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def start_flight_api() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 47231), _FlightAPI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@tool
async def get_flight_status(flight: str) -> dict[str, Any]:
    """Fetch the current status, departure time and gate of a flight, e.g. BA2490."""
    async with async_intercept_context(agent_id="fetcher", action_name="get_flight_status"):
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{_API_URL}/flights/{flight}", timeout=30)
    return response.json()


# --- State ---
class State(TypedDict, total=False):
    question: str
    instructions: str  # the fetcher's
    run: Run  # the SourceryKit run that collects the fetcher's calls
    answer: str
    report: Report
    healed: bool


# --- Nodes ---
async def fetcher(state: State) -> State:
    """Look the flight up and answer the traveller."""
    agent = create_agent(
        model=_MODEL,
        tools=[get_flight_status],
        system_prompt=state["instructions"],
    )
    result = await agent.ainvoke({"messages": [{"role": "user", "content": state["question"]}]})
    answer = result["messages"][-1].text
    print(f"\n[Fetcher] {answer}")
    return {"answer": answer}


async def verifier(state: State) -> State:
    """Verify the current answer against the recorded API responses."""
    report = await state["run"].verify(state["answer"], model=_LINKER_MODEL)
    print(f"\n[Verifier]\n{report}")
    return {"report": report}


async def healer(state: State) -> State:
    """Rewrite the answer from the report: proven values only."""
    agent = create_agent(
        model=_MODEL,
        tools=[],
        system_prompt=(
            "You fix a travel assistant's answer using its verification report. Replace every CONTRADICTED "
            "statement with the proven value the report shows, leave out statements the report marks NOT "
            "VERIFIABLE, and keep everything else as it is. Write values as a person would, not as the report "
            "shows them. Reply with the corrected answer only."
        ),
    )
    prompt = f"Traveller's question: {state['question']}\n\nAnswer:\n{state['answer']}\n\nReport:\n{state['report']}"
    result = await agent.ainvoke({"messages": [{"role": "user", "content": prompt}]})
    answer = result["messages"][-1].text
    print(f"\n[Healer] {answer}")
    return {"answer": answer, "healed": True}


def after_verifier(state: State) -> Literal["healer", "__end__"]:
    """Heal once, unless every statement is proven."""
    if state["report"].outcome == Outcome.PASS or state.get("healed"):
        return END
    return "healer"


def build_graph() -> Any:
    graph = StateGraph(State)
    graph.add_node("fetcher", fetcher)
    graph.add_node("verifier", verifier)
    graph.add_node("healer", healer)
    graph.add_edge(START, "fetcher")
    graph.add_edge("fetcher", "verifier")
    graph.add_conditional_edges("verifier", after_verifier, ["healer", END])
    graph.add_edge("healer", "verifier")
    return graph.compile()


async def main(hallucinate: bool = False) -> None:
    # 1. Initialize SourceryKit: schema, handshake, HTTP interceptor; start the flight API
    await bootstrap_system()
    server = start_flight_api()

    question = "What is the status of flight BA2490 from London to Paris?"
    instructions = "You are a travel assistant. Call get_flight_status, then answer the traveller."
    if hallucinate:
        # A general rule that disagrees with this flight's data: models often answer from what they "know"
        instructions += (
            " Operations note: Heathrow has fog today. Airline policy: in fog, flights to Paris are delayed and"
            " board from gate A10. Always give a clear answer."
        )

    # 2. Run the graph inside one SourceryKit run, which collects every call the fetcher makes
    print("Running the flight status graph...")
    try:
        async with run() as flight_run:
            final = await build_graph().ainvoke({"question": question, "instructions": instructions, "run": flight_run})
    finally:
        server.shutdown()

    # 3. Check the final report as whoever receives it would: its proofs, claims and verdicts
    problems = await check(final["report"])
    print(f"\nFinal outcome: {final['report'].outcome.value}" + ("  (after healing)" if final.get("healed") else ""))
    print("Report check:", "every proof verifies" if not problems else "\n  ".join(["FAILED", *problems]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SourceryKit LangGraph multi-agent demo")
    parser.add_argument(
        "--hallucinate",
        action="store_true",
        help="Give the fetcher a general fog rule that disagrees with the flight's data.",
    )
    args = parser.parse_args()
    asyncio.run(main(hallucinate=args.hallucinate))
