"""Convergent reuses native spans and keeps opt-in content out of stored traces."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from docsgpt import tracing
from docsgpt.core.settings import settings
from docsgpt.llm.deadlines import timed_stream
from docsgpt.tracing.content import capture, events, inputs, messages, request
from docsgpt.tracing.convergent import agent_trace
from docsgpt.tracing.otel import export_trace


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setattr(settings, "TRACES_ENABLED", True)
    monkeypatch.setattr(settings, "TRACES_OTEL_EXPORT", True)
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "span_only")
    for key in ("CONVERGENT_API_KEY", "CONVERGENT_SPANS_DIR", "CONVERGENT_TRACES_EXPORTER"):
        monkeypatch.delenv(key, raising=False)


def test_export_only_content_and_guardrail_suppression():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace = tracing.start_trace(source="test")
    with tracing.activate(trace):
        with tracing.span(tracing.KIND_AGENT, "agent") as span:
            request(span, [{"role": "user", "content": "private prompt"}])
            assert list(events(span, iter([{"answer": "private answer"}]))) == [{"answer": "private answer"}]
    trace.finish()
    assert "private prompt" not in str(trace.to_record())
    assert "private answer" not in str(trace.to_record())
    export_trace(trace, provider)
    recorded = next(s for s in exporter.get_finished_spans() if s.name == "agent")
    assert "private prompt" in recorded.attributes["gen_ai.input.messages"]
    assert "private answer" in recorded.attributes["convergent.output"]
    exporter.clear()
    trace.content_blocked = True
    export_trace(trace, provider)
    assert all("private" not in str(s.attributes) for s in exporter.get_finished_spans())
    provider.shutdown()


def test_content_opt_out(monkeypatch):
    monkeypatch.delenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT")
    trace = tracing.start_trace(source="test")
    with tracing.activate(trace):
        with tracing.span(tracing.KIND_AGENT, "agent") as span:
            capture(span, secret="hidden")
            list(events(span, iter([{"answer": "hidden"}])))
            assert span._otel_content == {}


def test_threaded_stream_keeps_real_agent_parent():
    trace = tracing.start_trace(source="test")
    def generate():
        with tracing.span(tracing.KIND_LLM, "chat"):
            yield "chunk"
    with tracing.activate(trace):
        with tracing.span(tracing.KIND_AGENT, "agent") as agent:
            assert list(timed_stream(generate, first_token=2, stall=2)) == ["chunk"]
    assert next(s for s in trace.spans if s.name == "chat").parent_id == agent.id


def test_input_sanitizes_file_parts_and_preserves_tool_calls():
    result = inputs([
        {"role": "system", "content": "instructions"},
        {"role": "user", "content": [{"type": "text", "text": "question"},
                                      {"type": "image_url", "image_url": {"url": "data:image/png;base64,SECRET"}}]},
        {"role": "assistant", "tool_calls": [{"id": "t1", "function": {"name": "lookup", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "found"},
    ])
    assert "SECRET" not in str(result)
    assert result[1]["parts"][0]["name"] == "lookup"
    assert result[2]["parts"][0]["id"] == "t1"


def test_streamed_tool_call_output_is_actual_response():
    def chunk(name, args, call_id=None):
        call = SimpleNamespace(index=0, id=call_id, function=SimpleNamespace(name=name, arguments=args))
        return SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[call]))
    result = messages([chunk("lookup", '{"q":', "t1"), chunk(None, '"test"}')])
    assert result[0]["parts"] == [{"type": "tool_call", "id": "t1", "name": "lookup", "arguments": {"q": "test"}}]


def test_unconfigured_sdk_does_not_create_or_replace_trace():
    with agent_trace():
        assert tracing.current_trace() is None
    trace = tracing.start_trace(source="request")
    with tracing.activate(trace), agent_trace():
        assert tracing.current_trace() is trace


def test_initialize_once_and_respect_release(monkeypatch):
    import convergent
    import docsgpt.tracing.convergent as bridge
    monkeypatch.setattr(bridge, "_initialized_pid", None)
    monkeypatch.setenv("CONVERGENT_SPANS_DIR", "unused")
    monkeypatch.setenv("CONVERGENT_RELEASE", "test-release")
    init = Mock(return_value=SimpleNamespace(enabled=True))
    monkeypatch.setattr(convergent, "init", init)
    assert bridge.initialize()
    assert bridge.initialize()
    init.assert_called_once_with(release="test-release")
