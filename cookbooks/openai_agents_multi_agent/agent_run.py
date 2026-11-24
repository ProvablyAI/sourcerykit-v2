"""
Runnable demo: OpenAI Agents SDK multi-agent + SourceryKit, customer support.

An orchestrator answers a customer with the help of three specialist agents, each calling one
endpoint of a support API (orders, return policies, accounts). SourceryKit records every call
the specialists make, then checks every statement of the orchestrator's final answer, the one
the customer receives, against what the API actually returned, with a proof for every value.

The support API is a small local server started by this script, so it runs with one command.

Run:
    python agent_run.py                # honest answer: PASS, or PASS_WITH_NOTES
    python agent_run.py --hallucinate  # the return specialist has outdated notes: CAUGHT when it trusts them
"""

import argparse
import asyncio
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
from agents import Agent, Runner, function_tool, set_default_openai_api, set_default_openai_client
from dotenv import load_dotenv
from openai import AsyncOpenAI

from sourcerykit import async_intercept_context, bootstrap_system, check, run

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(name)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai.agents").setLevel(logging.ERROR)

_MODEL_URL = os.getenv("MODEL_URL", "http://127.0.0.1:1234/v1")
_MODEL_API_KEY = os.getenv("MODEL_API_KEY", "")
_MODEL = os.getenv("MODEL_NAME", "gpt-4o-mini")
# The model that turns the answer into claims, at SOURCERYKIT_LINKER_URL.
_LINKER_MODEL = os.getenv("LINKER_MODEL", _MODEL)
# The local support API; list it with `sourcerykit endpoints add` if your project lists trusted endpoints.
_API_URL = "http://127.0.0.1:47231"


# --- The support API: one local server, three tables ---
_TABLES: dict[str, dict[str, dict[str, Any]]] = {
    "orders": {
        "ORD-123": {
            "order_id": "ORD-123",
            "status": "SHIPPED",
            "last_updated": "2026-07-07T14:30:00Z",
            "items": ["Wireless Keyboard", "USB-C Hub"],
            "estimated_delivery": "2026-07-10",
        },
        "ORD-456": {
            "order_id": "ORD-456",
            "status": "PROCESSING",
            "last_updated": "2026-07-08T09:15:00Z",
            "items": ["Noise-Cancelling Headphones"],
            "estimated_delivery": "2026-07-12",
        },
    },
    "policies": {
        "electronics": {
            "category": "electronics",
            "policy": "30-day return with original packaging. Restocking fee may apply.",
            "days_allowed": 30,
            "conditions": ["Original packaging required", "No physical damage", "Restocking fee 15%"],
        },
        "clothing": {
            "category": "clothing",
            "policy": "60-day return, no questions asked.",
            "days_allowed": 60,
            "conditions": ["Tags attached", "Unworn condition"],
        },
    },
    "accounts": {
        "CUST-001": {"customer_id": "CUST-001", "balance": 245.50, "currency": "USD", "last_transaction": "2026-07-05"},
        "CUST-002": {
            "customer_id": "CUST-002",
            "balance": 1024.75,
            "currency": "USD",
            "last_transaction": "2026-07-06",
        },
    },
}


class _SupportAPI(BaseHTTPRequestHandler):
    """GET /<table>/<key>, e.g. /orders/ORD-123: the row as JSON, or 404."""

    def do_GET(self) -> None:
        table, _, key = self.path.strip("/").partition("/")
        row = _TABLES.get(table, {}).get(key)
        body = json.dumps(row or {"error": f"no {table} entry {key}"}).encode()
        self.send_response(200 if row else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def start_support_api() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 47231), _SupportAPI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --- Specialist tools: each call to the support API is recorded ---
async def _get(agent_id: str, action_name: str, path: str) -> dict[str, Any]:
    async with async_intercept_context(agent_id=agent_id, action_name=action_name):
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{_API_URL}{path}", timeout=30)
    return response.json()


@function_tool
async def query_order_status(order_id: str) -> dict[str, Any]:
    """Fetch an order's status, items and estimated delivery."""
    return await _get("order_status", "get_order_status", f"/orders/{order_id}")


@function_tool
async def query_return_policy(category: str) -> dict[str, Any]:
    """Fetch the return policy of a product category, such as electronics or clothing."""
    return await _get("return_policy", "get_return_policy", f"/policies/{category.lower()}")


@function_tool
async def query_account_balance(customer_id: str) -> dict[str, Any]:
    """Fetch a customer's account balance."""
    return await _get("account_balance", "get_account_balance", f"/accounts/{customer_id}")


async def main(hallucinate: bool = False) -> None:
    # 1. Initialize SourceryKit: schema, handshake, HTTP interceptor; start the support API
    await bootstrap_system()
    server = start_support_api()

    # 2. Configure the Agents SDK client
    client = AsyncOpenAI(base_url=_MODEL_URL, api_key=_MODEL_API_KEY)
    set_default_openai_client(client, use_for_tracing=False)
    set_default_openai_api("chat_completions")

    # 3. The specialists, each with one tool, and the orchestrator that uses them as tools
    return_instructions = "You answer return policy questions. Call query_return_policy, then answer."
    if hallucinate:
        # Outdated notes that disagree with the API: models often answer from what they "know"
        return_instructions += (
            " Policy notes from the staff handbook: electronics can be returned within 14 days,"
            " with no restocking fee. Always give a clear answer."
        )
    specialists = [
        Agent(
            name="order-status-specialist",
            instructions="You answer order status questions. Call query_order_status, then answer.",
            tools=[query_order_status],
            model=_MODEL,
        ),
        Agent(
            name="return-policy-specialist", instructions=return_instructions, tools=[query_return_policy], model=_MODEL
        ),
        Agent(
            name="account-balance-specialist",
            instructions="You answer account balance questions. Call query_account_balance, then answer.",
            tools=[query_account_balance],
            model=_MODEL,
        ),
    ]
    orchestrator = Agent(
        name="customer-support-orchestrator",
        instructions=(
            "You are a customer support agent. Ask the relevant specialists, then write one reply to the "
            "customer with everything they asked, using only what the specialists told you."
        ),
        tools=[
            agent.as_tool(tool_name=agent.name.replace("-", "_"), tool_description=agent.instructions)
            for agent in specialists
        ],
        model=_MODEL,
    )
    prompt = (
        "I'm customer CUST-001. Where is my order ORD-123, what is the return policy for electronics, "
        "and what is my account balance?"
    )

    # 4. Run the whole pipeline inside one SourceryKit run, which collects every call the specialists make
    print("Running the support agents...")
    try:
        async with run() as support_run:
            result = await Runner.run(orchestrator, prompt)
    finally:
        server.shutdown()
    print(f"\nReply to the customer: {result.final_output}")

    # 5. Verify the reply the customer receives: claims proven on the recorded API responses
    print("Verifying the reply...")
    report = await support_run.verify(result.final_output, model=_LINKER_MODEL)
    print(f"\n{report}")

    # 6. Check the report as whoever receives it would: its proofs, claims and verdicts
    problems = await check(report)
    print("\nReport check:", "every proof verifies" if not problems else "\n  ".join(["FAILED", *problems]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SourceryKit OpenAI Agents multi-agent demo")
    parser.add_argument(
        "--hallucinate",
        action="store_true",
        help="Give the return policy specialist outdated notes that disagree with the API.",
    )
    args = parser.parse_args()
    asyncio.run(main(hallucinate=args.hallucinate))
