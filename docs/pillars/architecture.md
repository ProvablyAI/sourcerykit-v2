# Architecture
The SourceryKit SDK makes Python agents' answers verifiable: it records the HTTP calls an agent's tools make, then checks every statement of the agent's answer against what those calls returned, proving every value a verdict relies on. Each piece has one job, so it drops into an existing agent with a few lines.

## Core Flow
The interceptor records each call a tool makes; `run()` collects the calls of one agent run; `verify(answer)` asks the linker model how to check each statement, seals the run's responses in the facts table, and proves every query on them through the QEDB middleware. The report it returns can be checked by anyone with `check(report)`.

<div align="center">
  <img src="https://raw.githubusercontent.com/ProvablyAI/sourcerykit/refs/heads/main/docs/images/architecture.svg" alt="architecture" width="600" />
</div>

## Component Overview

- **Agent**: The host Python application and its tools. It makes HTTP requests as usual, runs inside `run()`, and passes its final text answer to `verify`.

- **Bootstrap System**: One-time initialization at startup: the database schema, the handshake with Provably that registers your tables with the QEDB middleware, and the HTTP interceptor. See [README](https://github.com/ProvablyAI/sourcerykit).

- **HTTP Interceptor**: Records the outbound HTTP calls made inside `async_intercept_context`, with the exact response text. See [interceptor](https://sourcerykit.provably.ai/docs/pillars/interceptor).

- **Run**: `async with run()` collects the ids of the calls recorded inside it, including those of the tasks it starts, so an answer is checked against its own run's calls.

- **Linker**: A model you choose reads the answer and the recorded responses, and writes how to check each statement. It never decides a verdict, and code guards reject a check that could pass a value the statement does not state. See [verification](https://sourcerykit.provably.ai/docs/pillars/verification).

- **Database Tables** (in your PostgreSQL database):
  - **Intercepts**: Every recorded request and response, with the response's SHA-256 hash.
  - **Facts**: The responses of the run being verified, flattened into rows of a fixed schema, so that a check on any response is a query on the same columns. The middleware seals the table before any query runs.
  - **Trusted Endpoints**: An optional list of the endpoints whose calls count as evidence. See [trusted-endpoints](https://sourcerykit.provably.ai/docs/pillars/trusted-endpoints).

- **QEDB Middleware**: Provably's prover. It seals the facts table and proves the result of every query a check runs on it; the proofs are about 1 KB each.

- **Report**: The outcome (`PASS`, `PASS_WITH_NOTES`, `CAUGHT` or `ERROR`), each statement's verdict with its proven values and proofs, and what could not be checked.

- **Public Verifier**: Provably's endpoint that checks a proof and returns the query and result it proves. `check(report)` uses it, with no Provably account, and recomputes every verdict.

---

**Next steps:**

- [Onboarding](https://sourcerykit.provably.ai/docs/getting_started/onboarding) — one-time account, credentials, and database setup
- [End-to-End Walkthrough](https://sourcerykit.provably.ai/docs/getting_started/end-to-end-walkthrough) — run the whole flow in code
- [Interceptor](https://sourcerykit.provably.ai/docs/pillars/interceptor) — how outbound HTTP calls are recorded
- [Verification](https://sourcerykit.provably.ai/docs/pillars/verification) — the linker, proofs, the report and `check`
- [Trusted Endpoints](https://sourcerykit.provably.ai/docs/pillars/trusted-endpoints) — which calls count as evidence
