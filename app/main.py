import json
import logging
import time
import uuid
from typing import Any, AsyncGenerator

import openai
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import settings

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

app = FastAPI(title="LLM Wrapper")
client = openai.AsyncOpenAI(api_key=settings.openai_api_key)

# Responses API parameters that are valid (Chat Completions extras are dropped silently)
_RESPONSES_API_PARAMS = {"temperature", "max_tokens", "top_p", "stream"}


def _build_mcp_tool() -> dict:
    tool: dict[str, Any] = {
        "type": "mcp",
        "server_label": settings.mcp_server_label,
        "server_url": settings.mcp_server_url,
        "require_approval": settings.mcp_require_approval,
    }
    if settings.mcp_api_key:
        tool["headers"] = {"Authorization": f"Bearer {settings.mcp_api_key}"}
    return tool


def _split_messages(messages: list[dict]) -> tuple[list[dict], str | None]:
    """Extract system messages as Responses API instructions; return remaining input messages."""
    instructions_parts: list[str] = []
    input_messages: list[dict] = []
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            instructions_parts.append(content if isinstance(content, str) else json.dumps(content))
        else:
            input_messages.append(msg)
    instructions = "\n".join(instructions_parts) if instructions_parts else None
    return input_messages, instructions


def _to_chat_completions(response: Any, model: str) -> dict:
    return {
        "id": f"chatcmpl-{response.id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": response.output_text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": response.usage.input_tokens if response.usage else 0,
            "completion_tokens": response.usage.output_tokens if response.usage else 0,
            "total_tokens": (
                (response.usage.input_tokens or 0) + (response.usage.output_tokens or 0)
                if response.usage
                else 0
            ),
        },
    }


async def _stream_chunks(openai_stream: Any, model: str) -> AsyncGenerator[str, None]:
    async for event in openai_stream:
        event_type = getattr(event, "type", None)
        if event_type == "response.output_text.delta":
            chunk = {
                "id": f"chatcmpl-{uuid.uuid4().hex}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": getattr(event, "delta", "")},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
        elif event_type == "response.completed":
            final_chunk = {
                "id": f"chatcmpl-{uuid.uuid4().hex}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(final_chunk)}\n\n"
            yield "data: [DONE]\n\n"
        # MCP tool-call events handled transparently by OpenAI; skip silently


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": settings.default_model,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "openai",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "Invalid JSON", "type": "invalid_request_error", "code": "invalid_json"}})

    messages: list[dict] = body.get("messages", [])
    model: str = body.get("model") or settings.default_model
    stream: bool = body.get("stream", False)
    temperature = body.get("temperature")
    max_tokens = body.get("max_tokens")

    input_messages, instructions = _split_messages(messages)
    mcp_tool = _build_mcp_tool()

    create_kwargs: dict[str, Any] = {
        "model": model,
        "input": input_messages,
        "tools": [mcp_tool],
        "stream": stream,
    }
    if instructions is not None:
        create_kwargs["instructions"] = instructions
    if temperature is not None:
        create_kwargs["temperature"] = temperature
    if max_tokens is not None:
        create_kwargs["max_output_tokens"] = max_tokens

    try:
        if stream:
            openai_stream = await client.responses.create(**create_kwargs)
            return StreamingResponse(
                _stream_chunks(openai_stream, model),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        else:
            response = await client.responses.create(**create_kwargs)
            return JSONResponse(_to_chat_completions(response, model))

    except openai.APIStatusError as e:
        logger.error("OpenAI API error %s: %s", e.status_code, e.message)
        raise HTTPException(
            status_code=e.status_code,
            detail={"error": {"message": e.message, "type": "api_error", "code": str(e.status_code)}},
        )
    except openai.APIError as e:
        logger.error("OpenAI error: %s", e)
        raise HTTPException(
            status_code=502,
            detail={"error": {"message": str(e), "type": "api_error", "code": "upstream_error"}},
        )
