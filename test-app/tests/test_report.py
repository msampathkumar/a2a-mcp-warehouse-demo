import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.report import per_attempt  # noqa: E402


def _ev(kind, path, **kw):
    return {
        'kind': kind,
        'run_id': f'r-{path}',
        'q_id': 'Q1',
        'trial': '1',
        'path': path,
        **kw,
    }


def test_counts_come_from_raw_events_and_skip_cache_hits():
    results = [
        {
            'run_id': 'r-mcp',
            'q_id': 'Q1',
            'path': 'mcp',
            'elapsed_ms': 1000,
            'correct': True,
        },
        {
            'run_id': 'r-a2a',
            'q_id': 'Q1',
            'path': 'a2a',
            'elapsed_ms': 2000,
            'correct': False,
        },
    ]
    client = [
        _ev(
            'tool_call',
            'mcp',
            tool='describe_orders',
            tool_kind='mcp',
            ms=0.1,
            cached=True,
        ),
        _ev(
            'tool_call',
            'mcp',
            tool='query_orders',
            tool_kind='mcp',
            ms=120.0,
            cached=False,
        ),
        _ev(
            'tool_call',
            'mcp',
            tool='local_sql',
            tool_kind='local',
            ms=3.0,
            cached=False,
        ),
        _ev('cache_hit', 'mcp', tool='describe_orders', via='mcp_tool'),
        _ev('llm_call', 'mcp', tokens_in=100, tokens_out=10),
        _ev(
            'tool_call',
            'a2a',
            tool='warehouse_analyst',
            tool_kind='a2a',
            ms=3000.0,
        ),
        _ev('llm_call', 'a2a', tokens_in=50, tokens_out=5),
    ]
    server = [
        _ev('dm_call', 'mcp', ok=True, trace_id='t1'),
        _ev('dm_call', 'a2a', ok=True, trace_id='t2'),
        _ev('dm_call', 'a2a', ok=False, trace_id='t2'),
        _ev('llm_call', 'a2a', tokens_in=500, tokens_out=50, trace_id='t2'),
        _ev('cache_hit', 'a2a', trace_id='t2'),
    ]
    m, a = per_attempt(results, client, server)
    assert (
        m['mcp_calls'],
        m['mcp_call_ms'],
        m['dm_calls_mcp'],
        m['client_cache_hits'],
    ) == (1, 120.0, 1, 1)
    assert (m['client_llm_calls'], m['client_tokens']) == (1, 110)
    assert (
        a['a2a_calls'],
        a['a2a_call_ms'],
        a['dm_calls_a2a'],
        a['dm_rejected'],
    ) == (1, 3000.0, 2, 1)
    assert (
        a['remote_llm_calls'],
        a['remote_tokens'],
        a['server_cache_hits'],
    ) == (1, 550, 1)
