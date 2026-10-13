from opentelemetry import baggage, context

from warehouse.config import Settings
from warehouse.workspace import Workspace


def _as(run_id, q_id='Q1'):
    ctx = baggage.set_baggage('run_id', run_id)
    ctx = baggage.set_baggage('q_id', q_id, context=ctx)
    return context.attach(ctx)


def _ws(dm, recorder, tmp_path, **kw):
    return Workspace(Settings(level=5, cache_dir=tmp_path, **kw), dm, recorder)


def _dm_calls(recorder, run_id):
    return [e for e in recorder.events(run_id) if e['kind'] == 'dm_call']


def test_cache_hit_skips_data_manager(dm_level5, recorder, tmp_path):
    ws = _ws(dm_level5, recorder, tmp_path)
    tok = _as('ws-cache')
    try:
        ws.query('stores', 'SELECT count(*) FROM stores')
        r = ws.query(
            'stores', 'select   count(*) from stores'
        )  # same after normalization
        assert r['cached'] is True
        assert len(_dm_calls(recorder, 'ws-cache')) == 1
        assert [e['kind'] for e in recorder.events('ws-cache')].count(
            'cache_hit'
        ) == 1
    finally:
        context.detach(tok)


def test_cache_off_means_every_call_hits_source(dm_level5, recorder, tmp_path):
    ws = _ws(dm_level5, recorder, tmp_path, cache_enabled=False)
    tok = _as('ws-nocache')
    try:
        ws.query('stores', 'SELECT count(*) FROM stores')
        ws.query('stores', 'SELECT count(*) FROM stores')
        assert len(_dm_calls(recorder, 'ws-nocache')) == 2
    finally:
        context.detach(tok)


def test_cache_off_frames_do_not_leak_across_questions(
    dm_level5, recorder, tmp_path
):
    ws = _ws(dm_level5, recorder, tmp_path, cache_ttl_min=0)
    tok = _as('ws-frames', 'Q1')
    ws.save_result('s', 'stores', 'SELECT id, name FROM stores')
    assert ws.local_sql('SELECT count(*) FROM s')['rows'] == [[6]]
    context.detach(tok)
    tok = _as('ws-frames', 'Q2')
    try:
        assert ws.list_workspace()['frames'] == []
    finally:
        context.detach(tok)


def test_save_result_pages_and_local_join(dm_level5, recorder, tmp_path):
    ws = _ws(dm_level5, recorder, tmp_path)
    tok = _as('ws-join')
    try:
        r = ws.save_result(
            'cust', 'customers', 'SELECT id, name FROM customers ORDER BY id'
        )
        assert r['rows'] == 2583 and r['pages'] == 6
        ws.save_result('st', 'stores', 'SELECT id, name FROM stores')
        ws.save_result(
            'by_store',
            'orders',
            'SELECT store_id, count(*) AS n FROM orders GROUP BY store_id',
        )
        out = ws.local_sql(
            'SELECT st.name, b.n FROM by_store b JOIN st ON b.store_id = st.id ORDER BY b.n DESC LIMIT 1'
        )
        assert out['rows'] == [['Brooklyn', 25502]]
        again = ws.save_result(
            'cust', 'customers', 'SELECT id, name FROM customers ORDER BY id'
        )
        assert again['cached'] is True
        assert 'error' in ws.local_sql("SELECT * FROM read_csv('/etc/passwd')")
    finally:
        context.detach(tok)


def test_reset_clears_scope(dm_level5, recorder, tmp_path):
    ws = _ws(dm_level5, recorder, tmp_path)
    tok = _as('ws-reset')
    try:
        ws.save_result('st', 'stores', 'SELECT id FROM stores')
        ws.write_notes('stores.id joins orders.store_id')
        assert ws.reset('ws-reset') == ['ws-reset']
        assert ws.list_workspace()['frames'] == []
        assert ws.read_notes()['notes'] == ''
    finally:
        context.detach(tok)


def test_notes_sandboxed(dm_level5, recorder, tmp_path):
    ws = _ws(dm_level5, recorder, tmp_path)
    tok = _as('../../etc')
    try:
        ws.write_notes('x')
        written = list(tmp_path.rglob('notes.md'))
        assert len(written) == 1 and tmp_path in written[0].parents
    finally:
        context.detach(tok)


def test_semi_join_fetch(dm_level5, recorder, tmp_path):
    ws = _ws(dm_level5, recorder, tmp_path)
    tok = _as("ws-semi")
    try:
        ws.save_result("day", "orders", "SELECT id FROM orders WHERE ordered_at >= '2021-08-02' AND ordered_at < '2021-08-03'")
        r = ws.semi_join_fetch("items", "order_items", "order_id, sku", "order_id", "day", "id")
        assert r["rows"] == 185  # matches answer key L4-Q04
        assert "error" in ws.semi_join_fetch("x", "order_items", "sku", "order_id", "missing", "id")
    finally:
        context.detach(tok)
