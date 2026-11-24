# Trusted Endpoints
Trusted endpoints decide which recorded calls can back an answer. They are optional: with none listed, every call recorded in a run is evidence. Once your project lists some, calls to any other endpoint are still recorded, but left out of verification and listed in the report as untrusted.

## When to List Them
List the endpoints you trust when a tool calls a URL the model can influence, such as a generic "fetch this page" tool. Without a list, a page the model chose could back its own answer; with one, only the APIs you listed can.

If every tool calls a fixed API, you need no list.

## Example
Register trusted endpoints during your application's setup, or once with the CLI:

```python
from sourcerykit import insert_trusted_endpoint

await insert_trusted_endpoint(url="https://api.open-meteo.com")
```

A run that also called another endpoint gets a report like:

```text
Outcome: PASS   (claims by linker:anthropic/claude-haiku-5.5)
  SUPPORTED      '6.5 °C'  proven: t=6.5000  u=°C
  UNTRUSTED      https://example.com/some-page: recorded, not used as evidence
```

## How the Registry Works
- **Per organization**: Endpoints are stored in the `trusted_endpoints` table of your database, for your Provably organization.
- **Matching**: An endpoint is stored as its scheme, host and path (a trailing `/` removed; a URL given without a scheme is stored as `https://`). A call is trusted when it has the same scheme, host and port, and the endpoint's path or a path under it. `https://api.example.com` trusts every path on that host; `https://api.example.com/v1/forecast` trusts `/v1/forecast`, `/v1/forecast/daily` and `/v1/forecast?city=x`, but not `/v1/forecasts`. A host that only starts with a listed one, such as `api.example.com.evil.net`, is not trusted.
- **Read at verification**: The list is read when `verify` runs, so changes apply to the next verification without a restart.

## Configuration

### CLI
```bash
sourcerykit endpoints add <url> [--label <label>]
sourcerykit endpoints list
sourcerykit endpoints remove <url>
```

### API
All functions are asynchronous.

#### Register a trusted endpoint
```python
await sourcerykit.insert_trusted_endpoint(*, url: str, display_label: str | None = None) -> None
```
Adds an endpoint to the registry. If it is already registered, nothing changes.

#### Check whether a URL is trusted
```python
await sourcerykit.trusted_endpoints.is_endpoint_trusted(url: str) -> bool
sourcerykit.trusted_endpoints.endpoint_matches(url: str, endpoint: str) -> bool  # the rule, for one endpoint
```

#### List the trusted endpoints
```python
await sourcerykit.trusted_endpoints.list_all_trusted_endpoints_detailed() -> list[dict[str, Any]]
```
Each entry has the endpoint's `url`, `label`, `policy_version` and `created_by`.

#### Remove a trusted endpoint
```python
await sourcerykit.trusted_endpoints.service.remove_trusted_endpoint(*, url: str) -> None
```

---

For how calls are recorded, see [interceptor](https://sourcerykit.provably.ai/docs/pillars/interceptor). For how the report lists untrusted calls, see [verification](https://sourcerykit.provably.ai/docs/pillars/verification).
