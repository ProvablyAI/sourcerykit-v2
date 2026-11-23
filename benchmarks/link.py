"""Runs the linker on every benchmark case and freezes its replies in links/<model>/.

Only this costs money; run.py then scores the frozen replies offline, as engine linker:<model>.

    MODEL_URL=... MODEL_API_KEY=... uv run python benchmarks/link.py anthropic/claude-haiku-4.5
"""

import asyncio
import json
import os
import sys
from typing import Any

import run
from openai import AsyncOpenAI

from sourcerykit import linker


async def link(client: AsyncOpenAI, model: str, case: dict[str, Any]) -> dict[str, Any]:
    calls = [(c.get("url", ""), json.dumps(c["response"], ensure_ascii=False)) for c in case["calls"]]
    completion = await client.chat.completions.create(
        model=model,
        messages=linker.messages(case["answer"], calls),  # type: ignore[arg-type]
        temperature=0,
    )
    usage = completion.usage
    return {
        "model": model,
        "reply": completion.choices[0].message.content,
        "tokens": None if usage is None else {"prompt": usage.prompt_tokens, "completion": usage.completion_tokens},
    }


async def main(model: str) -> None:
    client = AsyncOpenAI(base_url=os.environ["MODEL_URL"], api_key=os.environ["MODEL_API_KEY"])
    cases = run.load_cases()
    replies = await asyncio.gather(*(link(client, model, case) for case in cases))
    out = run.LINKS_DIR / model.replace("/", "_")
    out.mkdir(parents=True, exist_ok=True)
    for case, reply in zip(cases, replies, strict=True):
        (out / f"{case['id']}.json").write_text(json.dumps(reply, indent=2, ensure_ascii=False) + "\n")
    tokens = [r["tokens"] for r in replies if r["tokens"]]
    print(
        f"{out}: {len(replies)} cases, {sum(t['prompt'] for t in tokens)} prompt and "
        f"{sum(t['completion'] for t in tokens)} completion tokens"
    )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
