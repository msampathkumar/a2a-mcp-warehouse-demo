"""End-to-end tests against a real uvicorn server (no LLM calls)."""

import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from warehouse.app import create_app
from warehouse.config import Settings

TRACE_ID = '4bf92f3577b34da6a3ce929d0e0e4736'
HEADERS = {
    'traceparent': f'00-{TRACE_ID}-00f067aa0ba902b7-01',
    'baggage': 'run_id=test-run-1,q_id=L2-Q01,trial=1',
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    port = _free_port()
    settings = Settings(
        level=2,
        port=port,
        runs_dir=tmp_path_factory.mktemp('runs'),
        cache_dir=tmp_path_factory.mktemp('cache'),
        source_latency_ms=0,
    )
    app = create_app(settings)
    srv = uvicorn.Server(
        uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning')
    )
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    base = f'http://localhost:{port}'
    for _ in range(100):
        try:
            if httpx.get(f'{base}/healthz').status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield base, app
    srv.should_exit = True
    t.join(timeout=10)


def test_catalog_lists_one_mcp_server_per_source(server):
    base, _ = server
    cat = httpx.get(f'{base}/catalog.json').json()
    assert cat['level'] == 2 and cat['tables'] == ['orders', 'stores']
    assert [m['url'] for m in cat['mcp_servers']] == [
        f'{base}/mcp/orders/',
        f'{base}/mcp/stores/',
    ]
    assert (
        cat['a2a_agents'][0]['agent_card']
        == f'{base}/a2a/.well-known/agent-card.json'
    )


def test_agent_card_served(server):
    base, _ = server
    card = httpx.get(f'{base}/a2a/.well-known/agent-card.json').json()
    assert card['name'] == 'warehouse_analyst'
    assert '/a2a' in str(card)


def test_homepage_and_tally_have_no_answers(server):
    base, _ = server
    assert 'Warehouse challenge' in httpx.get(f'{base}/').text
    tally = httpx.get(f'{base}/tally.json').json()
    for q in tally['questions']:
        assert q['level'] == 2
        assert not {'answer', 'expected', 'reference_sql'} & set(q)


async def test_mcp_call_is_attributed_and_traced(server):
    base, app = server
    # Like the harness: the caller's trace is current, so the MCP SDK's own
    # _meta propagation and our HTTP headers carry the same trace.
    from opentelemetry import context, propagate

    token = context.attach(propagate.extract(HEADERS))
    http = create_mcp_http_client(headers=HEADERS)
    async with Client(
        streamable_http_client(f'{base}/mcp/stores/', http_client=http)
    ) as c:
        tools = sorted(t.name for t in (await c.list_tools()).tools)
        assert tools == ['describe_stores', 'query_stores']
        res = await c.call_tool(
            'query_stores', {'sql': 'SELECT count(*) AS n FROM stores'}
        )
        assert '"rows":[[6]]' in res.content[0].text
        bad = await c.call_tool('query_stores', {'sql': 'SELECT * FROM orders'})
        assert 'only serves' in bad.content[0].text
    context.detach(token)
    events = httpx.get(
        f'{base}/_stats', params={'run_id': 'test-run-1'}
    ).json()['events']
    calls = [e for e in events if e['kind'] == 'dm_call']
    assert [(e['path'], e['source'], e['ok']) for e in calls] == [
        ('mcp', 'stores', True),
        ('mcp', 'stores', False),
    ]
    assert all(e['q_id'] == 'L2-Q01' and e['trial'] == '1' for e in calls)
    assert all(e['trace_id'] == TRACE_ID for e in calls), (
        "DataManager span must join the caller's trace"
    )


def test_mcp_server_for_unloaded_level_absent(server):
    base, _ = server
    r = httpx.post(f'{base}/mcp/customers/', json={})
    assert r.status_code == 404
