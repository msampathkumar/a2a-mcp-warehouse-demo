"""Client-side tracing + event log for the harness.

Each (path, level, question, trial) runs under one root span. Its context and
W3C baggage (run_id, q_id, trial, path) are injected into every outgoing MCP
and A2A HTTP request, so server-side spans and DataManager events join it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Sequence

from opentelemetry import baggage, context, propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)


class JsonlSpanExporter(SpanExporter):
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        out = []
        for s in spans:
            c = s.get_span_context()
            out.append(
                json.dumps(
                    {
                        'name': s.name,
                        'trace_id': format(c.trace_id, '032x'),
                        'span_id': format(c.span_id, '016x'),
                        'parent_id': format(s.parent.span_id, '016x')
                        if s.parent
                        else None,
                        'service': s.resource.attributes.get('service.name'),
                        'start_ns': s.start_time,
                        'end_ns': s.end_time,
                        'duration_ms': round(
                            (s.end_time - s.start_time) / 1e6, 3
                        ),
                        'status': s.status.status_code.name,
                        'attributes': {
                            k: (
                                v
                                if isinstance(v, (str, int, float, bool))
                                else str(v)
                            )
                            for k, v in (s.attributes or {}).items()
                        },
                    },
                    separators=(',', ':'),
                )
            )
        with self._lock, self.path.open('a', encoding='utf-8') as f:
            f.write('\n'.join(out) + '\n')
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


def setup_tracing(spans_path: Path) -> None:
    provider = TracerProvider(
        resource=Resource.create({'service.name': 'harness-client'})
    )
    provider.add_span_processor(
        SimpleSpanProcessor(JsonlSpanExporter(spans_path))
    )
    if os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT'):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


tracer = trace.get_tracer('harness')


def outgoing_headers() -> dict[str, str]:
    """traceparent + baggage for the current context."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


async def httpx_inject_hook(request) -> None:
    for k, v in outgoing_headers().items():
        request.headers[k] = v


@contextmanager
def question_scope(run_id: str, q_id: str, trial: int, path: str, level: int):
    """Root span + baggage for one question attempt."""
    ctx = context.get_current()
    for k, v in {
        'run_id': run_id,
        'q_id': q_id,
        'trial': str(trial),
        'path': path,
    }.items():
        ctx = baggage.set_baggage(k, v, context=ctx)
    token = context.attach(ctx)
    try:
        with tracer.start_as_current_span(
            f'question {q_id} [{path}]',
            attributes={
                'run_id': run_id,
                'q_id': q_id,
                'trial': trial,
                'path': path,
                'level': level,
            },
        ) as span:
            yield span
    finally:
        context.detach(token)


class EventLog:
    """Client-observed events (tool calls, LLM calls, cache hits) for one run."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.events: list[dict] = []

    def record(self, kind: str, **fields: Any) -> None:
        e = {
            'ts': time.time(),
            'kind': kind,
            **{
                k: str(baggage.get_baggage(k) or '')
                for k in ('run_id', 'q_id', 'trial', 'path')
            },
            **fields,
        }
        with self._lock:
            self.events.append(e)
            with self.path.open('a', encoding='utf-8') as f:
                f.write(
                    json.dumps(e, separators=(',', ':'), default=str) + '\n'
                )
