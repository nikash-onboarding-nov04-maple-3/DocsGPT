# Convergent agent tracing

DocsGPT sends its existing OpenTelemetry GenAI spans to Convergent when
`CONVERGENT_API_KEY` is set. It does not install a second model instrumentor.
The same hooks cover `ClassicAgent` (including the legacy `react` alias),
`AgenticAgent`, `ResearchAgent`, `WorkflowAgent`, their workflow-node variants,
and resumed tool-approval turns.

## Enable

1. Create an **ingestion API key** in your Convergent workspace settings.
2. Set `CONVERGENT_API_KEY` in the API and worker environments, then restart
   those processes. Existing `TRACES_ENABLED` and `TRACES_OTEL_EXPORT` settings
   must remain enabled (both default to true).
3. To include prompts, model responses and final agent deliverables, explicitly
   set `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=span_only`.
   Without this opt-in, spans and usage are exported but content is withheld.

The SDK initializes lazily in the process executing the agent. Release defaults
to DocsGPT's version (`docsgpt/version.py`); `CONVERGENT_RELEASE` overrides it.
No release variable is required. Existing OpenTelemetry providers, samplers and
exporters are preserved. They will also receive opted-in message content.

For local-only inspection, set `CONVERGENT_SPANS_DIR` to a private directory
instead of an API key. Each process writes OTLP JSONL to `spans.jsonl` there.
Use a different directory per process when inspecting concurrent workers.
`CONVERGENT_STRICT=1` is useful in staging to fail on invalid SDK configuration.
The SDK's standard endpoint and attribute-filter environment variables also
apply; see the [Convergent SDK docs](https://github.com/convergent-technologies/convergent-sdk).

## Content and lifecycle

- One native agent span covers the complete generator, not just its creation.
  It has a stable class name, input query, and (when opted in) final answer,
  sources, tool-call records, metadata and status. Workflow node spans remain
  children of the workflow. Conversation IDs become session IDs when available.
- Model spans keep input/output messages and the existing token accounting.
  Streaming tool-call fragments are reconstructed as actual model output.
- Full message content exists only in memory until OTel replay. It is **not**
  added to the `request_traces` database record. Existing bounded, redacted
  previews remain unchanged. A guardrail that blocks trace content also
  suppresses the additional content at export time.
- Tool argument/result previews retain DocsGPT's existing redaction and size
  limit. They are not guaranteed to contain a whole large tool result.
- Inline non-text file blocks are replaced by placeholders in model inputs.
  This integration does **not** upload files/artifact bytes to Convergent;
  file transfers and generated-file previews are not verified.
- Existing request-owned traces are replayed by their normal sink. A direct
  agent call with no request trace gets a replay-only trace and flushes on
  completion/close, without introducing a database write. Abrupt process kills
  can still lose unfinished traces.

## Verification scope

Seven frozen original-source scenarios exercised every concrete agent class.
They used the real OpenAI client, prompts, response handlers and dispatcher with
scripted loopback responses. Storage-backed tool configuration and activity/
journal writes were fixtures, not a production database. Classic and agentic
runs executed the real `ThinkTool` and follow-up model request; research runs
covered clarification, planning, tool execution, follow-up and synthesis.
A workflow executed a real classic node, and every node variant also ran
individually. The classic and agentic node fixtures intentionally had no tools.

Checks compared final answers, actual tool outcomes and workflow state with the
original source, then inspected fresh OTLP spans for ancestry, content, usage,
release and duplicate calls. These checks do not validate live provider calls,
hosted ingestion/display, production storage, files, other providers, retries,
error branches, or resumed approval turns. Regression tests additionally cover
content opt-out, guardrail suppression, export-only content, SDK initialization
and propagation of the agent parent into the stream-reader thread.

Dependency: `convergent-sdk==0.0.11`, with the repository's locked OpenTelemetry
packages. The release tag `v0.0.11` did not contain `llms.txt`; the current public
SDK docs were read instead and used APIs were checked against the installed SDK.
