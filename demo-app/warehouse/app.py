"""Demo app: homepage + AI Catalog + N MCP servers + 1 A2A agent, for one LEVEL.

Run:  LEVEL=1 uvicorn --factory warehouse.app:create_app --port 8001   (from demo-app/)
"""

from __future__ import annotations

import datetime as dt
import hmac
import json
import logging
import time
from collections import defaultdict, deque
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from opentelemetry import trace
from starlette.routing import Mount

from .a2a_agent import build_agent
from .config import APP_DIR, Settings
from .data_manager import DataManager
from .mcp_servers import build_mcp_server
from .telemetry import Recorder, TraceContextMiddleware, setup_tracing
from .workspace import Workspace

log = logging.getLogger('warehouse')
STATIC = Path(__file__).parent / 'static'
TALLY = APP_DIR / 'tally.json'


class _ServerSpanMiddleware:
    """Open one server span per HTTP request, inside the caller's trace."""

    def __init__(self, app) -> None:
        self.app = app
        self.tracer = trace.get_tracer('warehouse.http')

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        path = scope.get('path', '')
        kind = (
            'mcp'
            if path.startswith('/mcp/')
            or any(
                path == f'/{t}' or path.startswith(f'/{t}/')
                for t in ('orders', 'stores', 'customers', 'order_items', 'products')
            )
            else 'a2a'
            if path.startswith('/a2a')
            else 'web'
        )
        with self.tracer.start_as_current_span(
            f'{scope.get("method", "GET")} {path}',
            kind=trace.SpanKind.SERVER,
            attributes={'http.route': path, 'warehouse.entry': kind},
        ):
            await self.app(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or Settings()
    s.runs_dir.mkdir(parents=True, exist_ok=True)
    setup_tracing(
        f'warehouse-level-{s.level}',
        s.runs_dir / f'level{s.level}_spans.jsonl',
        cloud_logging=s.cloud_logging,
    )
    recorder = Recorder(
        s.runs_dir / f'level{s.level}_events.jsonl',
        cloud_logging=s.cloud_logging,
        bq_table=s.bq_telemetry_table,
        bq_location=s.bq_location,
    )
    dm = DataManager(
        s.data_dir,
        s.tables,
        recorder,
        s.row_cap,
        s.source_latency_ms,
        isolated_warehouses=s.isolated_warehouses,
    )
    ws = Workspace(s, dm, recorder, path='a2a')

    mcp_apps = {
        t: build_mcp_server(t, dm).streamable_http_app(
            streamable_http_path='/',
            stateless_http=True,
            json_response=True,
            host='0.0.0.0',
        )
        for t in s.tables
    }
    port = s.effective_port
    a2a_app = None
    if s.a2a_enabled:
        if s.public_url:
            u = urlparse(s.public_url)
            a2a_host = u.hostname or 'localhost'
            a2a_port = u.port or (443 if u.scheme == 'https' else 80)
            a2a_protocol = u.scheme or 'https'
        else:
            a2a_host, a2a_port, a2a_protocol = 'localhost', port, 'http'

        a2a_app = to_a2a(
            build_agent(s, ws, recorder),
            host=a2a_host,
            port=a2a_port,
            protocol=a2a_protocol,
            rpc_path='a2a',
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with AsyncExitStack() as stack:
            sub_apps = [*mcp_apps.values(), *([a2a_app] if a2a_app else [])]
            for sub in sub_apps:
                await stack.enter_async_context(
                    sub.router.lifespan_context(sub)
                )
            log.info(
                'level %s ready: tables=%s port=%s a2a=%s',
                s.level,
                s.tables,
                port,
                s.a2a_enabled,
            )
            yield

    app = FastAPI(
        title=f'A2A + MCP warehouse demo (level {s.level})', lifespan=lifespan
    )
    app.state.settings, app.state.dm, app.state.ws, app.state.recorder = (
        s,
        dm,
        ws,
        recorder,
    )

    def _check_admin(token: str | None) -> None:
        if s.admin_token and not hmac.compare_digest(
            token or '', s.admin_token
        ):
            raise HTTPException(status_code=401, detail='admin token required')

    @app.get('/', include_in_schema=False)
    async def home():
        return FileResponse(STATIC / 'index.html')

    @app.get('/healthz')
    async def healthz():
        return {'ok': True, 'level': s.level}

    @app.get('/catalog.json')
    async def catalog():
        base = s.base_url
        schemas = dm.schemas()
        mcp_subpath = '' if s.public_demo else '/mcp'
        return {
            'name': 'Sandwich-shop data warehouse',
            'level': s.level,
            'tables': s.tables,
            'isolated_warehouses': s.isolated_warehouses,
            'mcp_servers': [
                {
                    'name': f'warehouse-{t}',
                    'warehouse_db': f'{t}.duckdb',
                    'table': t,
                    'columns': len(schemas[t]['cols']),
                    'rows': schemas[t]['total'],
                    'transport': 'streamable-http',
                    'url': f'{base}{mcp_subpath}/{t}/',
                    'tools': [f'describe_{t}', f'query_{t}'],
                }
                for t in s.tables
            ],
            'a2a_agents': (
                [
                    {
                        'name': 'warehouse_analyst',
                        'agent_card': f'{base}/a2a/.well-known/agent-card.json',
                        'url': f'{base}/a2a/',
                    }
                ]
                if s.a2a_enabled
                else []
            ),
            'rules': {
                'one_table_per_query': True,
                'isolated_warehouse_per_mcp': s.isolated_warehouses,
                'row_cap': s.row_cap,
                'source_latency_ms': s.source_latency_ms,
            },
            'config': s.public_dict(),
        }

    @app.get('/metrics.json')
    async def metrics():
        """Fast O(1) in-memory MCP call metrics for homepage refreshes (never touches DuckDB)."""
        return recorder.public_metrics(s.tables)

    @app.get('/tally.json')
    async def tally():
        """Questions for this level (or all 50 questions on the public demo). Never contains answers."""
        data = (
            json.loads(TALLY.read_text())
            if TALLY.exists()
            else {'questions': []}
        )
        all_qs = data.get('questions', [])
        qs = all_qs if s.public_demo else [q for q in all_qs if q.get('level') == s.level]
        for q in qs:
            if 'answer' in q or 'expected' in q or 'reference_sql' in q:
                raise HTTPException(
                    status_code=500,
                    detail='tally.json must not contain answers',
                )
        return {
            'level': s.level,
            'questions': qs,
            'generated_at': data.get('generated_at'),
        }

    @app.get('/_stats')
    async def stats(
        run_id: str = Query('manual'), x_admin_token: str | None = Header(None)
    ):
        _check_admin(x_admin_token)
        events = recorder.events(run_id)
        return JSONResponse(
            {
                'run_id': run_id,
                'level': s.level,
                'config': s.public_dict(),
                'events': events,
            }
        )

    @app.post('/_reset')
    async def reset(
        run_id: str | None = Query(None),
        x_admin_token: str | None = Header(None),
    ):
        if s.public_demo:
            raise HTTPException(
                status_code=403, detail='reset disabled on public demo'
            )
        _check_admin(x_admin_token)
        return {'reset': ws.reset(run_id)}

    for t, sub in mcp_apps.items():
        app.router.routes.append(Mount(f'/mcp/{t}', app=sub))
        app.router.routes.append(Mount(f'/{t}', app=sub))
    if a2a_app is not None:
        app.router.routes.append(
            Mount('/', app=a2a_app)
        )  # serves /a2a/ and its agent card; keep last

    if s.public_demo:
        app.add_middleware(_A2aRateLimitMiddleware, settings=s, recorder=recorder)
    app.add_middleware(_ServerSpanMiddleware)
    app.add_middleware(TraceContextMiddleware)  # added last = runs first
    return app


class _A2aRateLimitMiddleware:
    """Per-IP and global daily rate limit on POST /a2a* when PUBLIC_DEMO=true."""

    def __init__(self, app, *, settings: Settings, recorder: Recorder) -> None:
        self.app = app
        self.settings = settings
        self.recorder = recorder
        self._ip_hits: dict[str, deque[float]] = defaultdict(deque)
        self._day: dt.date = dt.datetime.now(dt.timezone.utc).date()
        self._day_count: int = 0

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        path = scope.get('path', '')
        method = scope.get('method', 'GET').upper()
        if method == 'POST' and path.startswith('/a2a'):
            now = time.monotonic()
            today = dt.datetime.now(dt.timezone.utc).date()
            if today != self._day:
                self._day = today
                self._day_count = 0
            if self._day_count >= self.settings.a2a_daily_cap:
                self.recorder.record('rate_limited', path='a2a', reason='daily_cap')
                res = JSONResponse(
                    {'error': 'Daily public demo A2A quota reached. Try again tomorrow.'},
                    status_code=429,
                )
                return await res(scope, receive, send)
            headers = dict(scope.get('headers') or [])
            xff = headers.get(b'x-forwarded-for', b'').decode('latin1')
            client_ip = (
                xff.split(',')[0].strip()
                if xff
                else (scope.get('client') or ('unknown', 0))[0]
            )
            q = self._ip_hits[client_ip]
            while q and now - q[0] > 60.0:
                q.popleft()
            if len(q) >= self.settings.a2a_rate_limit_per_min:
                self.recorder.record(
                    'rate_limited', path='a2a', reason='per_ip', ip=client_ip
                )
                res = JSONResponse(
                    {'error': 'Rate limit exceeded: max A2A requests per minute reached.'},
                    status_code=429,
                )
                return await res(scope, receive, send)
            q.append(now)
            self._day_count += 1
        await self.app(scope, receive, send)

