"""DeepSeek V4 Flash: the YES/NO gate and the tool-calling reply loop."""

import json
import os

from openai import AsyncOpenAI

import memory
import tools
from prompts import GATE_INSTRUCTION, REPLY_INSTRUCTION

MODEL = "deepseek-v4-flash"
MAX_TOOL_ROUNDS = 6

_client: AsyncOpenAI | None = None


def _ds() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")
    return _client


def build_messages(transcript: str, system: str, instruction: str) -> list:
    # Same prefix for gate and reply calls, so DeepSeek's prefix cache carries over.
    # Two personas simply occupy two cache entries; the instruction is last, not first,
    # so each persona still shares its own prefix between gate and reply.
    ledger = memory.render_for_prompt()
    if ledger:
        system += "\n\n## Household ledger (your memories)\n" + ledger
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "Recent group conversation:\n" + transcript},
        {"role": "user", "content": instruction},
    ]


async def gate(transcript: str, system: str, name: str) -> bool:
    resp = await _ds().chat.completions.create(
        model=MODEL,
        messages=build_messages(transcript, system, GATE_INSTRUCTION.format(name=name)),
        tools=tools.TOOL_SCHEMAS,  # keeps the request prefix identical to respond()
        tool_choice="none",
        max_tokens=4,
        temperature=0,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return "YES" in (resp.choices[0].message.content or "").upper()


async def respond(transcript: str, system: str, name: str, chained: bool = False) -> tuple[str, list[bytes]]:
    ctx = tools.ToolCtx()
    schemas = tools.CHAINED_SCHEMAS if chained else tools.TOOL_SCHEMAS  # no unbidden images mid-chain
    messages = build_messages(transcript, system, REPLY_INSTRUCTION.format(name=name))
    for _ in range(MAX_TOOL_ROUNDS):
        resp = await _ds().chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=schemas,
            max_tokens=1500,
            extra_body={"thinking": {"type": "enabled"}},
        )
        msg = resp.choices[0].message
        messages.append(msg)  # as-is, preserving reasoning_content per DeepSeek docs
        if not msg.tool_calls:
            return (msg.content or "").strip(), ctx.images
        for call in msg.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await tools.execute(call.function.name, args, ctx)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    return "I appear to have lost my train of thought, sir.", ctx.images
