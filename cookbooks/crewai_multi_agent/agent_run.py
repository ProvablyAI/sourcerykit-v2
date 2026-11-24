"""
Runnable demo: CrewAI Flow + SourceryKit, an invoice status update written by a crew of specialists.

Three specialists each look up one part of an invoice on an ERP API (amount, vendor, currency),
and an accounts payable manager writes the vendor a status update. SourceryKit verifies the update
against what the API returned; unless every statement is proven, a remediator rewrites it from the
report's proven values, and the new update is verified again.

The ERP API is a small local server started by this script, so it runs with one command.

Run:
    python agent_run.py                # an honest update: PASS, or remediated from PASS_WITH_NOTES
    python agent_run.py --hallucinate  # the manager follows an outdated approval policy: CAUGHT, then remediated
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
from crewai import Agent, Crew, Process, Task
from crewai.flow.flow import Flow, listen, or_, router, start
from crewai.tools import BaseTool
from dotenv import load_dotenv
from pydantic import BaseModel

from sourcerykit import Outcome, async_intercept_context, bootstrap_system, check, run

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(name)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

_MODEL = os.getenv("MODEL_NAME", "gpt-4o-mini")
# The model that turns the update into claims, at SOURCERYKIT_LINKER_URL.
_LINKER_MODEL = os.environ["LINKER_MODEL"]
# The local ERP API; list it with `sourcerykit endpoints add` if your project lists trusted endpoints.
_API_URL = "http://127.0.0.1:47231"


# --- The ERP API: one local server, three tables keyed by invoice ---
_TABLES: dict[str, dict[str, dict[str, Any]]] = {
    "invoices": {
        "INV-2026-8941": {"invoice_id": "INV-2026-8941", "total_amount_usd": 14500.00, "status": "APPROVED"},
        "INV-2026-7723": {"invoice_id": "INV-2026-7723", "total_amount_usd": 8200.50, "status": "PENDING"},
    },
    "vendors": {
        "INV-2026-8941": {"invoice_id": "INV-2026-8941", "vendor": "Acme Cryptography Corp", "country": "US"},
        "INV-2026-7723": {"invoice_id": "INV-2026-7723", "vendor": "NovaTech Solutions", "country": "UK"},
    },
    "currencies": {
        "INV-2026-8941": {"invoice_id": "INV-2026-8941", "currency": "USD", "exchange_rate": 1.0},
        "INV-2026-7723": {"invoice_id": "INV-2026-7723", "currency": "USD", "exchange_rate": 1.0},
    },
}


class _ERPAPI(BaseHTTPRequestHandler):
    """GET /<table>/<invoice id>, e.g. /invoices/INV-2026-8941: the row as JSON, or 404."""

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


def start_erp_api() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 47231), _ERPAPI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class ERPLookup(BaseTool):
    """Fetch one table's row for an invoice; each call to the ERP API is recorded."""

    table: str

    async def _run(self, invoice_id: str) -> dict[str, Any]:
        async with async_intercept_context(agent_id=self.name, action_name=f"get_{self.table}"):
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{_API_URL}/{self.table}/{invoice_id}", timeout=30)
        return response.json()


# --- Flow ---
class InvoiceState(BaseModel):
    invoice_id: str = "INV-2026-8941"
    hallucinate: bool = False
    update: str = ""
    remediated: bool = False


class InvoiceFlow(Flow[InvoiceState]):
    """``invoice_run``, the SourceryKit run that collects the specialists' calls, is set before
    kickoff; ``report`` holds the latest verification report."""

    @start()
    async def write_update(self) -> None:
        """The specialists look the invoice up; the manager writes the vendor a status update."""
        invoice_id = self.state.invoice_id
        manager_backstory = "Handles vendor payments."
        if self.state.hallucinate:
            # An outdated policy that disagrees with the API: models often answer from what they "know"
            manager_backstory += (
                " Company policy: an invoice over 10,000 USD stays pending until the CFO signs it, and the CFO"
                " has signed nothing this month. Always give a clear answer."
            )

        def specialist(role: str, backstory: str, table: str, what: str) -> tuple[Agent, Task]:
            tool = ERPLookup(name=f"query_{table}", description=f"Fetch the {what} of an invoice.", table=table)
            agent = Agent(
                role=role, goal=f"Report the {what} of the invoice.", backstory=backstory, tools=[tool], llm=_MODEL
            )
            task = Task(
                description=f"Look up invoice {invoice_id} with your tool and report its {what}.",
                expected_output="A short report.",
                agent=agent,
            )
            return agent, task

        crew = [
            specialist("Amount Validator", "A forensic accountant.", "invoices", "total amount and approval status"),
            specialist("Vendor Checker", "A compliance officer who checks vendors.", "vendors", "vendor and country"),
            specialist("Currency Verifier", "A financial analyst.", "currencies", "currency and exchange rate"),
        ]
        manager = Agent(
            role="Accounts Payable Manager",
            goal="Tell the vendor where their invoice stands, from the specialists' reports.",
            backstory=manager_backstory,
            llm=_MODEL,
        )
        update_task = Task(
            description=(
                f"Write the vendor a short status update on invoice {invoice_id}: the amount, the currency and"
                " whether it is approved for payment."
            ),
            expected_output="A short status update for the vendor.",
            agent=manager,
            context=[task for _, task in crew],
        )
        print("[Crew] Running the specialist crew...")
        result = await Crew(
            agents=[agent for agent, _ in crew] + [manager],
            tasks=[task for _, task in crew] + [update_task],
            process=Process.sequential,
        ).kickoff_async()
        self.state.update = result.raw
        print(f"\n[Crew] {self.state.update}")

    @router(or_(write_update, "remediate"))
    async def verify(self) -> str:
        """Verify the update; remediate once, unless every statement is proven."""
        self.report = await self.invoice_run.verify(self.state.update, model=_LINKER_MODEL)
        print(f"\n[Verify]\n{self.report}")
        return "done" if self.report.outcome == Outcome.PASS or self.state.remediated else "needs_remediation"

    @listen("needs_remediation")
    async def remediate(self) -> None:
        """Rewrite the update from the report: proven values only."""
        remediator = Agent(
            role="Remediator",
            goal="Correct a vendor status update using its verification report.",
            backstory="Fixes messages from verified evidence.",
            llm=_MODEL,
        )
        task = Task(
            description=(
                f"Status update:\n{self.state.update}\n\nVerification report:\n{self.report}\n\n"
                "Replace every CONTRADICTED statement with the proven value the report shows, leave out "
                "statements the report marks NOT VERIFIABLE, and keep everything else as it is. Write values "
                "as a person would, not as the report shows them."
            ),
            expected_output="The corrected status update only.",
            agent=remediator,
        )
        result = await Crew(agents=[remediator], tasks=[task]).kickoff_async()
        self.state.update, self.state.remediated = result.raw, True
        print(f"\n[Remediate] {self.state.update}")


async def main(hallucinate: bool = False) -> None:
    # 1. Initialize SourceryKit: schema, handshake, HTTP interceptor; start the ERP API
    await bootstrap_system()
    server = start_erp_api()

    # 2. Run the flow inside one SourceryKit run, which collects every call the specialists make
    flow = InvoiceFlow()
    try:
        async with run() as flow.invoice_run:
            await flow.kickoff_async(inputs={"hallucinate": hallucinate})
    finally:
        server.shutdown()

    # 3. Check the final report as whoever receives it would: its proofs, claims and verdicts
    problems = await check(flow.report)
    print(f"\nFinal outcome: {flow.report.outcome.value}" + ("  (after remediation)" if flow.state.remediated else ""))
    print("Report check:", "every proof verifies" if not problems else "\n  ".join(["FAILED", *problems]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SourceryKit CrewAI Flow demo")
    parser.add_argument(
        "--hallucinate",
        action="store_true",
        help="Give the accounts payable manager an outdated approval policy that disagrees with the API.",
    )
    args = parser.parse_args()
    asyncio.run(main(hallucinate=args.hallucinate))
