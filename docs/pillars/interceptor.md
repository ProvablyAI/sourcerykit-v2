# Interceptor
The Interceptor records the HTTP calls your agent's tools make. It hooks into outbound HTTP calls without changes to your tools' HTTP code, and stores each request with the exact response text, which is what [verification](https://sourcerykit.provably.ai/docs/pillars/verification) checks an answer against.

## Core Functions
`bootstrap_system()` patches the popular Python HTTP libraries (`httpx`, `aiohttp` and `requests`) for the whole process:

- **Recording**: A call made inside `async_intercept_context` is stored in the `intercepts` table: the URL, the method, the request parameters and body, the response text exactly as received, and its SHA-256 hash.
- **Scoping**: `run()` collects the ids of the calls recorded inside it, including those made by the tasks it starts, so `verify` checks an answer against the calls of that run only.

Calls made outside an intercept context are not recorded, and neither are SourceryKit's own calls (to Provably and to the linker model).

## Example

```python
import httpx
import sourcerykit

# Once, at startup: schema, handshake, HTTP interceptor
await sourcerykit.bootstrap_system()


async def get_data() -> dict:
    # Inside a tool: the calls made in this block are recorded, tagged with the agent and action
    async with sourcerykit.async_intercept_context(agent_id="demo", action_name="get_data"):
        async with httpx.AsyncClient() as client:
            response = await client.get("https://api.example.com/data", params={"query": "example"})
    return response.json()  # the tool returns the plain response: nothing to add for verification


async with sourcerykit.run() as r:
    data = await get_data()  # or run an agent whose tools call get_data
print(r.calls)  # the ids of the recorded calls
```

## Key Features

- **Context-aware tagging**: `agent_id` and `action_name` are stored with every call, mapping raw traffic to the agent action that made it. They label the calls; which calls an answer is checked against is set by `run()`.
- **Any number of calls**: The same tool can be called many times in one run; verification tells the calls apart by their URL and content.
- **Process-wide interception**: Once initialized, every instance of a supported HTTP library in the process is covered.
- **Streams**: A streamed `httpx` response is recorded once it has been read to the end.

## Limitations

- **Supported libraries**: Only calls made through `httpx`, `aiohttp` and `requests` are recorded.
- **Streams through `aiohttp` and `requests`**: A streamed response (an event stream, a chunked `aiohttp` response, or `requests` with `stream=True`) is recorded as a placeholder, not its content, so it cannot back a statement. Use `httpx` for those calls.
- **JSON responses**: Any response is recorded, but verification reads JSON: a response that is not JSON gives no facts, so statements about it are not verifiable.

To keep calls to some endpoints out of verification, see [trusted-endpoints](https://sourcerykit.provably.ai/docs/pillars/trusted-endpoints). To see how the recorded calls are used, see [verification](https://sourcerykit.provably.ai/docs/pillars/verification).
