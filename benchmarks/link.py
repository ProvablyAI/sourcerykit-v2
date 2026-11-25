"""Runs the linker on every benchmark case and freezes its replies in links/<model>/.

Only this costs money; run.py then scores the frozen replies offline, as engine linker:<model>.
The model is called exactly as ``Run.verify`` calls it, from the same settings, so the scores are
those of what users run.

    SOURCERYKIT_LINKER_URL=... SOURCERYKIT_LINKER_API_KEY=... \
        uv run python benchmarks/link.py anthropic/claude-haiku-5.5
"""

import asyncio
import json
import os
import sys
from typing import Any

import run

from sourcerykit import linker
from sourcerykit.verify import _complete


async def link(model: str, case: dict[str, Any]) -> dict[str, Any]:
    calls = [(c.get("url", ""), json.dumps(c["response"], ensure_ascii=False)) for c in case["calls"]]
    url, key = os.environ["SOURCERYKIT_LINKER_URL"], os.environ.get("SOURCERYKIT_LINKER_API_KEY", "")
    return {"model": model, "reply": await _complete(linker.messages(case["answer"], calls), model, url, key)}


async def main(model: str) -> None:
    cases = run.load_cases()
    replies = await asyncio.gather(*(link(model, case) for case in cases))
    out = run.LINKS_DIR / model.replace("/", "_")
    out.mkdir(parents=True, exist_ok=True)
    for case, reply in zip(cases, replies, strict=True):
        (out / f"{case['id']}.json").write_text(json.dumps(reply, indent=2, ensure_ascii=False) + "\n")
    print(f"{out}: {len(replies)} cases")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
