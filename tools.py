"""Tool schemas, Alfred's four tools, and the Gemini media calls."""

import asyncio
import base64
import os

import httpx

import memory

IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gemini-3.1-flash-image")
VISION_MODEL = os.getenv("VISION_MODEL", "gemini-3.6-flash")
VISION_PROMPT = (
    "Describe this for a chat transcript: one or two plain sentences, no preamble. "
    "Quote any legible text verbatim. Answer in English."
)


class ToolCtx:
    """Carries side-band results (generated images) out of the tool loop."""

    def __init__(self) -> None:
        self.images: list[bytes] = []


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "tavily_search",
            "description": "Search the web for current facts. Use before answering anything you are not certain of.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Generate an image; it is attached to your reply automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed English description of the image"}
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Save a lasting fact about the household to your ledger.",
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string", "description": "The fact, one concise sentence"}},
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget",
            "description": "Delete a ledger entry by its id (the [#n] number).",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer", "description": "Memory id"}},
                "required": ["id"],
            },
        },
    },
]


# Mid-chain nobody asked for a picture, and at ~$0.05 each an unbounded bot-to-bot chain
# of them is the one genuinely runaway cost here. Humans can still ask either persona.
CHAINED_SCHEMAS = [s for s in TOOL_SCHEMAS if s["function"]["name"] != "generate_image"]


async def execute(name: str, args: dict, ctx: ToolCtx) -> str:
    try:
        if name == "tavily_search":
            return await _tavily(str(args["query"]))
        if name == "generate_image":
            ctx.images.append(await asyncio.to_thread(_generate_image, str(args["prompt"])))
            return "Image generated; it will be attached to your reply."
        if name == "remember":
            mem_id = memory.add(str(args["content"]).strip())
            return "That was already in the ledger." if mem_id is None else f"Noted (memory #{mem_id})."
        if name == "forget":
            return "Forgotten." if memory.delete(int(args["id"])) else "No such entry."
        return f"Unknown tool: {name}"
    except Exception as exc:  # the loop must survive any tool failure
        return f"Tool error: {exc}"


async def _tavily(query: str) -> str:
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {os.environ['TAVILY_API_KEY']}"},
            json={"query": query, "search_depth": "basic", "max_results": 5, "include_answer": True},
        )
        resp.raise_for_status()
    data = resp.json()
    parts = []
    if data.get("answer"):
        parts.append(f"Summary: {data['answer']}")
    for r in data.get("results", []):
        parts.append(f"{r.get('title', '')} ({r.get('url', '')}): {r.get('content', '')[:400]}")
    return "\n\n".join(parts) or "No results."


def _generate_image(prompt: str) -> bytes:
    from google import genai  # deferred: slow import, only needed here

    client = genai.Client()
    if hasattr(client, "interactions"):
        interaction = client.interactions.create(model=IMAGE_MODEL, input=prompt)
        image = getattr(interaction, "output_image", None)
        if image is not None and getattr(image, "data", None):
            return _as_bytes(image.data)
        for step in getattr(interaction, "steps", None) or []:
            for block in getattr(step, "content", None) or []:
                if getattr(block, "data", None):
                    return _as_bytes(block.data)
        raise RuntimeError("the image model returned no image")
    # older google-genai without the Interactions API
    response = client.models.generate_content(model=IMAGE_MODEL, contents=prompt)
    for part in response.candidates[0].content.parts:
        blob = getattr(part, "inline_data", None)
        if blob is not None and blob.data:
            return _as_bytes(blob.data)
    raise RuntimeError("the image model returned no image")


async def describe_media(data: bytes, mime_type: str) -> str:
    return await asyncio.to_thread(_describe_media, data, mime_type)


def _describe_media(data: bytes, mime_type: str) -> str:
    from google import genai  # deferred: slow import, only needed here
    from google.genai import types

    client = genai.Client()  # must stay bound: Client.__del__ closes the httpx pool
    resp = client.models.generate_content(
        model=VISION_MODEL,
        contents=[types.Part.from_bytes(data=data, mime_type=mime_type), VISION_PROMPT],
    )
    # Flattened: history is newline-joined, so a multi-line note would forge transcript lines.
    return " ".join((resp.text or "").split())[:400]


def _as_bytes(data) -> bytes:
    return data if isinstance(data, bytes) else base64.b64decode(data)
