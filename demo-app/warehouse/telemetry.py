"""OpenTelemetry setup, trace-context propagation, and the server-side event recorder.

* Spans go to a JSONL file (source of truth) and, if OTEL_EXPORTER_OTLP_ENDPOINT
  is set, to an OTLP collector such as local Jaeger.
* Callers send W3C `traceparent` + `baggage` headers. The ASGI middleware below
  makes that context current for the whole request, so DataManager calls made
  deep inside MCP tools or the ADK agent join the caller's trace.
* `Recorder` keeps one structured event per DataManager call / cache hit / LLM
  call. Counts in the report come from these events, never from the agents.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
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

RUN_KEYS = ('run_id', 'q_id', 'trial', 'path')
MANUAL = 'manual'


class JsonlSpanExporter(SpanExporter):
    """Write each finished span as one JSON line."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        lines = []
        for s in spans:
            ctx = s.get_span_context()
            lines.append(
                json.dumps(
                    {
                        'name': s.name,
                        'trace_id': format(ctx.trace_id, '032x'),
                        'span_id': format(ctx.span_id, '016x'),
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
                            k: _jsonable(v)
                            for k, v in (s.attributes or {}).items()
                        },
                    },
                    separators=(',', ':'),
                    default=str,
                )
            )
        with self._lock, self._path.open('a', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:  # nothing to release
        pass


def _jsonable(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return list(v) if isinstance(v, (list, tuple)) else str(v)


_provider_lock = threading.Lock()
_configured = False


def setup_tracing(service_name: str, spans_path: Path) -> None:
    """Install a global TracerProvider once per process."""
    global _configured
    with _provider_lock:
        if _configured:
            return
        provider = TracerProvider(
            resource=Resource.create({'service.name': service_name})
        )
        provider.add_span_processor(
            SimpleSpanProcessor(JsonlSpanExporter(spans_path))
        )
        if os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT'):
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        # TODO(deploy): OTEL_EXPORTER=gcp -> Cloud Trace exporter.
        trace.set_tracer_provider(provider)
        _configured = True


tracer = trace.get_tracer('warehouse')


def run_attrs() -> dict[str, str]:
    """run_id / q_id / trial from the caller's W3C baggage; 'manual' if absent."""
    out = {}
    for key in ('run_id', 'q_id', 'trial'):
        val = baggage.get_baggage(key)
        out[key] = str(val) if val is not None else MANUAL
    return out


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, '032x') if ctx.is_valid else None


class TraceContextMiddleware:
    """Pure ASGI middleware: make the caller's traceparent/baggage current."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        carrier = {
            k.decode('latin-1'): v.decode('latin-1')
            for k, v in scope.get('headers', [])
        }
        token = context.attach(propagate.extract(carrier))
        try:
            await self.app(scope, receive, send)
        finally:
            context.detach(token)


class Recorder:
    """Thread-safe store of server-side events, keyed by run_id, mirrored to JSONL."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._by_run: dict[str, list[dict]] = defaultdict(list)

    def record(self, kind: str, **fields: Any) -> dict:
        event = {
            'ts': time.time(),
            'kind': kind,
            **run_attrs(),
            'trace_id': current_trace_id(),
            **fields,
        }
        line = json.dumps(event, separators=(',', ':'), default=str)
        with self._lock:
            self._by_run[event['run_id']].append(event)
            with self._path.open('a', encoding='utf-8') as f:
                f.write(line + '\n')
        return event

    def events(self, run_id: str) -> list[dict]:
        with self._lock:
            return list(self._by_run.get(run_id, []))

    def run_ids(self) -> list[str]:
        with self._lock:
            return list(self._by_run)
