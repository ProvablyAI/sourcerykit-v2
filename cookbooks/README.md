# Cookbooks

Runnable examples of SourceryKit with popular agent frameworks. Each runs with `python agent_run.py`; add `--hallucinate` to give the agent a realistic reason to state something the data does not back. Every agent answers in plain text: SourceryKit records its tools' API calls inside `run()`, and `verify(answer)` checks each statement against them, with a proof for every value.

## Single-Agent Examples

| Framework | Description | Domain |
|---|---|---|
| [OpenAI Agents SDK](https://sourcerykit.provably.ai/docs/cookbooks/openai_agents) | One agent, one tool; the report and its fields explained | Weather |
| [LangChain](https://sourcerykit.provably.ai/docs/cookbooks/langchain_agent) | `create_agent`, answer from the last message | Weather |
| [Claude Agent SDK](https://sourcerykit.provably.ai/docs/cookbooks/claude_agent) | In-process MCP tool, answer from `ResultMessage` | Weather |
| [Claude Agent SDK](https://sourcerykit.provably.ai/docs/cookbooks/claude_agent_multi_tool) | One tool called for two cities; each value proven from its own call | Weather |

## Multi-Agent Examples

| Framework | Description | Domain | Pattern |
|---|---|---|---|
| [OpenAI Agents SDK](https://sourcerykit.provably.ai/docs/cookbooks/openai_agents_multi_agent) | Specialists as tools of an orchestrator; the final reply verified | Customer support | Orchestrator → Specialists → Verify |
| [LangGraph](https://sourcerykit.provably.ai/docs/cookbooks/langgraph_multi_agent) | Conditional routing on the outcome | Flight status | Fetcher → Verifier → Healer → Verifier |
| [CrewAI](https://sourcerykit.provably.ai/docs/cookbooks/crewai_multi_agent) | Flow with a specialist crew and a router | Invoice status | Crew → Verify → Remediate → Verify |

The multi-agent examples start a small local API on `http://127.0.0.1:47231`. If your project lists trusted endpoints, add it with `sourcerykit endpoints add http://127.0.0.1:47231`.

## Choosing an Example

- **Single-agent:** start with [openai_agents](https://sourcerykit.provably.ai/docs/cookbooks/openai_agents); its README explains the report.
- **Several calls to the same tool:** [claude_agent_multi_tool](https://sourcerykit.provably.ai/docs/cookbooks/claude_agent_multi_tool).
- **Multi-agent, verify the final answer:** [openai_agents_multi_agent](https://sourcerykit.provably.ai/docs/cookbooks/openai_agents_multi_agent).
- **Multi-agent, act on the outcome:** [langgraph_multi_agent](https://sourcerykit.provably.ai/docs/cookbooks/langgraph_multi_agent) or [crewai_multi_agent](https://sourcerykit.provably.ai/docs/cookbooks/crewai_multi_agent).
