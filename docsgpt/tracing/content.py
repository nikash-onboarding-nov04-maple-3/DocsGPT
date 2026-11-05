"""Opt-in, export-only message content. Never persisted in request_traces."""
from __future__ import annotations

import json
from typing import Any

from docsgpt.tracing.otel import content_capture_enabled


def capture(span: Any, **attributes: Any) -> None:
    """Keep content until replay; a blocked trace drops it at export time."""
    if not span or span.trace.content_blocked or not content_capture_enabled():
        return
    content = getattr(span, "_otel_content", None)
    if content is None:
        content = span._otel_content = {}
    content.update({key: value for key, value in attributes.items() if value is not None})


def messages(value: Any, role: str = "assistant") -> list[dict]:
    """Convert DocsGPT's text and OpenAI-compatible chunks to GenAI messages."""
    if isinstance(value, str):
        return [{"role": role, "parts": [{"type": "text", "content": value}]}]
    parts: list[dict] = []
    calls: dict[Any, dict] = {}
    for chunk in value if isinstance(value, (list, tuple)) else [value]:
        if isinstance(chunk, str):
            if parts and parts[-1].get("type") == "text":
                parts[-1]["content"] += chunk
            else:
                parts.append({"type": "text", "content": chunk})
            continue
        message = getattr(chunk, "delta", None) or getattr(chunk, "message", None)
        if message is None:
            continue
        text = getattr(message, "content", None)
        if isinstance(text, str) and text:
            parts.append({"type": "text", "content": text})
        for call in getattr(message, "tool_calls", None) or []:
            key = getattr(call, "index", None)
            if key is None:
                key = getattr(call, "id", None)
            entry = calls.setdefault(key, {"type": "tool_call", "id": "", "name": "", "arguments": ""})
            entry["id"] = getattr(call, "id", None) or entry["id"]
            function = getattr(call, "function", None)
            if function is not None:
                entry["name"] += getattr(function, "name", None) or ""
                entry["arguments"] += getattr(function, "arguments", None) or ""
    for call in calls.values():
        try:
            call["arguments"] = json.loads(call["arguments"])
        except (ValueError, TypeError):
            pass
        parts.append(call)
    return [{"role": role, "parts": parts}]


def inputs(value: list[dict]) -> list[dict]:
    """Capture text/tool messages without inline file bytes or data URLs."""
    result = []
    for message in value:
        role = message.get("role", "user")
        if role in ("system", "developer"):
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(
                part.get("text", "") if part.get("type") == "text" else "[file or non-text content]"
                for part in content if isinstance(part, dict)
            )
        parts = messages(content, role)[0]["parts"] if isinstance(content, str) else []
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            arguments = function.get("arguments", "")
            try:
                arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            except ValueError:
                pass
            parts.append({"type": "tool_call", "id": call.get("id"),
                          "name": function.get("name"), "arguments": arguments})
        if role == "tool":
            parts = [{"type": "tool_call_response", "id": message.get("tool_call_id"), "response": content}]
        if parts:
            result.append({"role": role, "parts": parts})
    return result


def request(span: Any, value: list[dict], tools: Any = None) -> None:
    """Record exactly one request's text, instructions and offered tool schemas."""
    if not span or not content_capture_enabled():
        return
    system = "\n".join(m["content"] for m in value
                       if m.get("role") in ("system", "developer") and isinstance(m.get("content"), str))
    capture(span, **{
        "gen_ai.input.messages": inputs(value),
        "gen_ai.system_instructions": system or None,
        "gen_ai.tool.definitions": tools or None,
    })


def events(span: Any, stream: Any) -> Any:
    """Forward agent events unchanged, collecting the user-visible deliverable only on opt-in."""
    if not span or not content_capture_enabled():
        yield from stream
        return
    answer: list[str] = []
    terminal: dict = {}
    status = "cancelled"
    try:
        for event in stream:
            if isinstance(event, dict):
                if isinstance(event.get("answer"), str):
                    answer.append(event["answer"])
                for key in ("sources", "tool_calls", "metadata", "error"):
                    if key in event:
                        terminal[key] = event[key]
            yield event
        status = "error" if "error" in terminal else "ok"
    except Exception:
        status = "error"
        raise
    finally:
        capture(span, **{
            "gen_ai.output.messages": messages("".join(answer)),
            "convergent.output": {"answer": "".join(answer), "status": status, **terminal},
        })
