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

import datetime as dt
import json
import logging
import os
import queue
import sys
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Sequence

import httpx
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


def _emit_cloud_log(severity: str, message: str, **payload: Any) -> None:
    """Write one structured JSON log line to stdout for Cloud Run / Cloud Logging."""
    project = os.environ.get('GOOGLE_CLOUD_PROJECT', '')
    trace_id = payload.get('trace_id') or current_trace_id()
    entry: dict[str, Any] = {
        'severity': severity,
        'message': message,
        **payload,
    }
    if trace_id and project:
        entry['logging.googleapis.com/trace'] = (
            f'projects/{project}/traces/{trace_id}'
        )
    sys.stdout.write(
        json.dumps(entry, separators=(',', ':'), default=str) + '\n'
    )
    sys.stdout.flush()


class JsonlSpanExporter(SpanExporter):
    """Write each finished span as one JSON line."""

    def __init__(self, path: Path, *, cloud_logging: bool = False) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cloud_logging = cloud_logging

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        lines = []
        for s in spans:
            ctx = s.get_span_context()
            span_dict = {
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
            }
            lines.append(
                json.dumps(span_dict, separators=(',', ':'), default=str)
            )
            if self._cloud_logging:
                _emit_cloud_log(
                    'WARNING' if s.status.status_code.name == 'ERROR' else 'INFO',
                    f"span {s.name} ({span_dict['duration_ms']} ms)",
                    telemetry_type='span',
                    **span_dict,
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


def setup_tracing(
    service_name: str, spans_path: Path, *, cloud_logging: bool = False
) -> None:
    """Install a global TracerProvider once per process."""
    global _configured
    with _provider_lock:
        if _configured:
            return
        provider = TracerProvider(
            resource=Resource.create({'service.name': service_name})
        )
        provider.add_span_processor(
            SimpleSpanProcessor(
                JsonlSpanExporter(spans_path, cloud_logging=cloud_logging)
            )
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


log = logging.getLogger('warehouse.telemetry')


def _parse_bq_table(spec: str) -> tuple[str, str, str] | None:
    """Parse 'project.dataset.table' or 'project:dataset.table' into a 3-tuple."""
    cleaned = spec.strip().replace(':', '.')
    if not cleaned:
        return None
    parts = [p for p in cleaned.split('.') if p]
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


class Recorder:
    """Thread-safe store of server-side events, keyed by run_id, mirrored to JSONL.

    Also maintains O(1) in-memory metrics (total calls, 24h/7d buckets, per-warehouse
    breakdown) so homepage refreshes never touch DuckDB or scan event logs.
    When `bq_table` is set (`project.dataset.table`), hydrates historical counts from
    BigQuery on startup and streams every new MCP `dm_call` event to BigQuery in a
    background daemon thread so metrics survive Cloud Run scale-to-zero.
    """

    def __init__(
        self,
        path: Path,
        *,
        cloud_logging: bool = False,
        bq_table: str = '',
        bq_location: str = 'us-central1',
    ) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._by_run: dict[str, list[dict]] = defaultdict(list)
        self._cloud_logging = cloud_logging
        self._started_at = time.time()
        self._total_mcp_calls = 0
        self._total_mcp_errors = 0
        self._by_source: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                'calls': 0,
                'describe_calls': 0,
                'query_calls': 0,
                'errors': 0,
                'rows_returned': 0,
                'total_ms': 0.0,
            }
        )
        self._daily_counts: dict[str, int] = defaultdict(int)
        self._daily_by_source: dict[str, dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        self._recent_ts: deque[float] = deque(maxlen=50_000)
        self._bq_target = _parse_bq_table(bq_table)
        self._bq_location = bq_location or 'us-central1'
        self._bq_queue: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        self._bq_creds = None
        self._bq_creds_lock = threading.Lock()
        if self._bq_target:
            self._hydrate_from_bq()
            t = threading.Thread(
                target=self._bq_worker, name='warehouse-bq-writer', daemon=True
            )
            t.start()

    def _get_bq_headers(self, project: str) -> dict[str, str]:
        with self._bq_creds_lock:
            if self._bq_creds is None:
                import google.auth

                self._bq_creds, _ = google.auth.default(
                    scopes=['https://www.googleapis.com/auth/cloud-platform']
                )
            if not self._bq_creds.valid or self._bq_creds.expired:
                from google.auth.transport.requests import Request

                self._bq_creds.refresh(Request())
            token = self._bq_creds.token
        return {
            'Authorization': f'Bearer {token}',
            'x-goog-user-project': project,
        }

    def _hydrate_from_bq(self) -> bool:
        """Load historical MCP metrics from BigQuery on container startup."""
        if not self._bq_target:
            return False
        project, dataset, table = self._bq_target
        sql = f"""
        WITH dedup AS (
          SELECT
            event_id,
            ts,
            source,
            op,
            ok,
            COALESCE(`rows`, 0) AS rows_n,
            COALESCE(ms, 0.0) AS ms_val
          FROM `{project}.{dataset}.{table}`
          QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY ts DESC) = 1
        ),
        by_src AS (
          SELECT
            source,
            COUNT(*) AS calls,
            COUNTIF(op = 'describe') AS describe_calls,
            COUNTIF(op != 'describe') AS query_calls,
            COUNTIF(NOT ok) AS errors,
            SUM(rows_n) AS rows_returned,
            SUM(ms_val) AS total_ms
          FROM dedup
          GROUP BY source
        ),
        daily AS (
          SELECT
            FORMAT_DATE('%Y-%m-%d', DATE(ts, 'UTC')) AS day_key,
            source,
            COUNT(*) AS calls
          FROM dedup
          WHERE ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 8 DAY)
          GROUP BY day_key, source
        ),
        recent_24h AS (
          SELECT UNIX_SECONDS(ts) AS ts_sec
          FROM dedup
          WHERE ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
        )
        SELECT TO_JSON_STRING(STRUCT(
          (SELECT COUNT(*) FROM dedup) AS total_calls,
          (SELECT COUNTIF(NOT ok) FROM dedup) AS total_errors,
          ARRAY(SELECT AS STRUCT * FROM by_src) AS by_source,
          ARRAY(SELECT AS STRUCT * FROM daily) AS daily,
          ARRAY(SELECT ts_sec FROM recent_24h) AS recent_ts
        )) AS payload
        """
        try:
            url = f'https://bigquery.googleapis.com/bigquery/v2/projects/{project}/queries'
            resp = httpx.post(
                url,
                headers=self._get_bq_headers(project),
                json={
                    'query': sql,
                    'useLegacySql': False,
                    'location': self._bq_location,
                    'timeoutMs': 10000,
                },
                timeout=12.0,
            )
            if resp.status_code != 200:
                log.warning('BigQuery hydration HTTP %s: %s', resp.status_code, resp.text[:200])
                return False
            data = resp.json()
            rows = data.get('rows') or []
            if not rows:
                return True
            payload = json.loads(rows[0]['f'][0]['v'])
            self._apply_hydrated_payload(payload)
            log.info(
                'Hydrated %s MCP calls (%s errors) from BigQuery %s.%s.%s',
                self._total_mcp_calls,
                self._total_mcp_errors,
                project,
                dataset,
                table,
            )
            return True
        except Exception as exc:
            log.warning('BigQuery hydration skipped: %s', exc)
            return False

    def _apply_hydrated_payload(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._total_mcp_calls = int(payload.get('total_calls') or 0)
            self._total_mcp_errors = int(payload.get('total_errors') or 0)
            self._by_source.clear()
            for item in payload.get('by_source') or []:
                src = str(item.get('source') or 'unknown')
                self._by_source[src] = {
                    'calls': int(item.get('calls') or 0),
                    'describe_calls': int(item.get('describe_calls') or 0),
                    'query_calls': int(item.get('query_calls') or 0),
                    'errors': int(item.get('errors') or 0),
                    'rows_returned': int(item.get('rows_returned') or 0),
                    'total_ms': float(item.get('total_ms') or 0.0),
                }
            self._daily_counts.clear()
            self._daily_by_source.clear()
            for d_item in payload.get('daily') or []:
                day_key = str(d_item.get('day_key') or '')
                src = str(d_item.get('source') or 'unknown')
                cnt = int(d_item.get('calls') or 0)
                if day_key:
                    self._daily_counts[day_key] += cnt
                    self._daily_by_source[day_key][src] += cnt
            self._recent_ts.clear()
            for ts_sec in sorted(payload.get('recent_ts') or []):
                self._recent_ts.append(float(ts_sec))

    def _bq_worker(self) -> None:
        """Background daemon worker that streams MCP events to BigQuery."""
        if not self._bq_target:
            return
        project, dataset, table = self._bq_target
        url = (
            f'https://bigquery.googleapis.com/bigquery/v2/projects/{project}'
            f'/datasets/{dataset}/tables/{table}/insertAll'
        )
        while True:
            first = self._bq_queue.get()
            batch = [first]
            while len(batch) < 50:
                try:
                    batch.append(self._bq_queue.get_nowait())
                except queue.Empty:
                    break
            try:
                resp = httpx.post(
                    url,
                    headers=self._get_bq_headers(project),
                    json={'rows': batch},
                    timeout=10.0,
                )
                if resp.status_code != 200:
                    log.warning(
                        'BigQuery insertAll HTTP %s: %s',
                        resp.status_code,
                        resp.text[:200],
                    )
            except Exception as exc:
                log.warning('BigQuery insertAll error: %s', exc)

    def record(self, kind: str, **fields: Any) -> dict:
        now = time.time()
        event_id = uuid.uuid4().hex
        event = {
            'event_id': event_id,
            'ts': now,
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
            if kind == 'dm_call' and fields.get('path') == 'mcp':
                src = str(fields.get('source') or 'unknown')
                op = str(fields.get('op') or 'query')
                ok = bool(fields.get('ok', True))
                rows = int(fields.get('rows') or 0)
                ms = float(fields.get('ms') or 0.0)
                day_key = (
                    dt.datetime.fromtimestamp(now, tz=dt.timezone.utc)
                    .date()
                    .isoformat()
                )
                self._total_mcp_calls += 1
                if not ok:
                    self._total_mcp_errors += 1
                st = self._by_source[src]
                st['calls'] += 1
                if op == 'describe':
                    st['describe_calls'] += 1
                else:
                    st['query_calls'] += 1
                if not ok:
                    st['errors'] += 1
                st['rows_returned'] += rows
                st['total_ms'] += ms
                self._daily_counts[day_key] += 1
                self._daily_by_source[day_key][src] += 1
                self._recent_ts.append(now)
                if self._bq_target:
                    ts_iso = dt.datetime.fromtimestamp(
                        now, tz=dt.timezone.utc
                    ).isoformat()
                    self._bq_queue.put(
                        {
                            'insertId': event_id,
                            'json': {
                                'event_id': event_id,
                                'ts': ts_iso,
                                'source': src,
                                'op': op,
                                'ok': ok,
                                'rows': rows,
                                'ms': ms,
                                'page': (
                                    int(fields['page'])
                                    if fields.get('page') is not None
                                    else None
                                ),
                                'sql': fields.get('sql'),
                                'error': fields.get('error'),
                                'run_id': str(event.get('run_id') or MANUAL),
                                'q_id': str(event.get('q_id') or MANUAL),
                                'trial': str(event.get('trial') or MANUAL),
                                'trace_id': event.get('trace_id'),
                            },
                        }
                    )
        if self._cloud_logging:
            sev = 'WARNING' if fields.get('ok') is False else 'INFO'
            msg = f"warehouse.{kind} source={fields.get('source', '-')} op={fields.get('op', '-')} ok={fields.get('ok', True)}"
            _emit_cloud_log(sev, msg, telemetry_type='event', **event)
        return event

    def public_metrics(self, sources: list[str]) -> dict[str, Any]:
        """Fast O(1) read of aggregated MCP metrics for the homepage."""
        now = time.time()
        today = dt.datetime.fromtimestamp(now, tz=dt.timezone.utc).date()
        last_7_days = [
            (today - dt.timedelta(days=i)).isoformat() for i in range(6, -1, -1)
        ]
        cutoff_24h = now - 86400.0
        with self._lock:
            calls_7d = sum(self._daily_counts.get(d, 0) for d in last_7_days)
            calls_24h = sum(1 for ts in self._recent_ts if ts >= cutoff_24h)
            per_warehouse = {}
            for s in sources:
                st = self._by_source.get(s)
                calls = st['calls'] if st else 0
                total_ms = st['total_ms'] if st else 0.0
                s_7d = sum(
                    self._daily_by_source[d].get(s, 0) for d in last_7_days
                )
                per_warehouse[s] = {
                    'calls_total': calls,
                    'calls_7d': s_7d,
                    'describe_calls': st['describe_calls'] if st else 0,
                    'query_calls': st['query_calls'] if st else 0,
                    'errors': st['errors'] if st else 0,
                    'rows_returned': st['rows_returned'] if st else 0,
                    'avg_ms': round(total_ms / calls, 1) if calls else 0.0,
                }
            daily_series = [
                {'date': d, 'calls': self._daily_counts.get(d, 0)}
                for d in last_7_days
            ]
            return {
                'started_at': dt.datetime.fromtimestamp(
                    self._started_at, tz=dt.timezone.utc
                ).isoformat(timespec='seconds'),
                'uptime_seconds': int(now - self._started_at),
                'bq_backed': bool(self._bq_target),
                'total_mcp_calls': self._total_mcp_calls,
                'calls_last_7d': calls_7d,
                'calls_last_24h': calls_24h,
                'total_errors': self._total_mcp_errors,
                'per_warehouse': per_warehouse,
                'daily_7d': daily_series,
            }

    def events(self, run_id: str) -> list[dict]:
        with self._lock:
            return list(self._by_run.get(run_id, []))

    def run_ids(self) -> list[str]:
        with self._lock:
            return list(self._by_run)
