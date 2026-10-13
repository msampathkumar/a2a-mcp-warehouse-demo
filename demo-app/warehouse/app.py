"""Demo app: homepage + AI Catalog + N MCP servers + 1 A2A agent, for one LEVEL.

Run:  LEVEL=1 uvicorn --factory warehouse.app:create_app --port 8001   (from demo-app/)
"""

from __future__ import annotations

import hmac
import json
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

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
        f'warehouse-level-{s.level}', s.runs_dir / f'level{s.level}_spans.jsonl'
    )
    recorder = Recorder(s.runs_dir / f'level{s.level}_events.jsonl')
    dm = DataManager(
        s.data_dir, s.tables, recorder, s.row_cap, s.source_latency_ms
    )
    ws = Workspace(s, dm, recorder, path='a2a')

    mcp_apps = {
        t: build_mcp_server(t, dm).streamable_http_app(
            streamable_http_path='/', stateless_http=True, json_response=True
        )
        for t in s.tables
    }
    port = s.effective_port
    a2a_app = to_a2a(
        build_agent(s, ws, recorder),
        host='localhost',
        port=port,
        rpc_path='a2a',
    )
    # TODO(deploy): when PUBLIC_URL is set, pass a pre-built AgentCard with that URL.

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with AsyncExitStack() as stack:
            for sub in [*mcp_apps.values(), a2a_app]:
                await stack.enter_async_context(
                    sub.router.lifespan_context(sub)
                )
            log.info(
                'level %s ready: tables=%s port=%s', s.level, s.tables, port
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
        return {
            'name': 'Sandwich-shop data warehouse',
            'level': s.level,
            'tables': s.tables,
            'mcp_servers': [
                {
                    'name': f'warehouse-{t}',
                    'transport': 'streamable-http',
                    'url': f'{base}/mcp/{t}/',
                    'tools': [f'describe_{t}', f'query_{t}'],
                }
                for t in s.tables
            ],
            'a2a_agents': [
                {
                    'name': 'warehouse_analyst',
                    'agent_card': f'{base}/a2a/.well-known/agent-card.json',
                    'url': f'{base}/a2a/',
                }
            ],
            'rules': {
                'one_table_per_query': True,
                'row_cap': s.row_cap,
                'source_latency_ms': s.source_latency_ms,
            },
            'config': s.public_dict(),
        }

    @app.get('/tally.json')
    async def tally():
        """Questions for this level. Never contains answers (they live in test-app/)."""
        data = (
            json.loads(TALLY.read_text())
            if TALLY.exists()
            else {'questions': []}
        )
        qs = [q for q in data.get('questions', []) if q.get('level') == s.level]
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
        _check_admin(x_admin_token)
        # TODO(deploy): disable entirely when PUBLIC_DEMO=true.
        return {'reset': ws.reset(run_id)}

    for t, sub in mcp_apps.items():
        app.router.routes.append(Mount(f'/mcp/{t}', app=sub))
    app.router.routes.append(
        Mount('/', app=a2a_app)
    )  # serves /a2a/ and its agent card; keep last

    app.add_middleware(_ServerSpanMiddleware)
    app.add_middleware(TraceContextMiddleware)  # added last = runs first
    return app

