"""Connect the existing GenAI replay to Convergent, without a second instrumentor."""
from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Iterator

from docsgpt.core.settings import settings
from docsgpt.tracing import core
from docsgpt.version import get_version

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_initialized_pid: int | None = None


def initialize() -> bool:
    """Initialize lazily in the process that runs the agent (including Celery children)."""
    global _initialized_pid
    if not settings.TRACES_ENABLED or not settings.TRACES_OTEL_EXPORT:
        return False
    if not any(os.getenv(key) for key in (
        "CONVERGENT_API_KEY", "CONVERGENT_SPANS_DIR", "CONVERGENT_TRACES_EXPORTER",
    )):
        return False
    with _lock:
        if _initialized_pid != os.getpid():
            import convergent

            status = convergent.init(release=os.getenv("CONVERGENT_RELEASE") or get_version())
            if not status.enabled:
                return False
            _initialized_pid = os.getpid()
    return True


@contextmanager
def agent_trace() -> Iterator[None]:
    """Reuse request traces; give direct/headless calls a replay-only trace of their own."""
    enabled = initialize()
    if not enabled or core.current_trace() is not None:
        yield
        return
    trace = core.start_trace(source="agent")
    with core.activate(trace):
        try:
            yield
        finally:
            if trace is not None:
                trace.finish()
                # No database persistence here: the request pipeline owns that sink.
                try:
                    import convergent
                    from docsgpt.tracing.otel import export_trace

                    export_trace(trace)
                    convergent.flush()
                except Exception:
                    logger.warning("Failed to export agent trace to Convergent", exc_info=True)


def _after_fork() -> None:
    global _lock, _initialized_pid
    _lock = threading.Lock()
    _initialized_pid = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)
