import pytest


def test_level1_only_loads_orders(dm_level1):
    assert dm_level1.tables == ['orders']
    r = dm_level1.query('stores', 'SELECT * FROM stores', path='mcp')
    assert 'unknown source' in r['error']


def test_describe(dm_level5):
    r = dm_level5.describe('stores', path='mcp')
    assert r['t'] == 'stores'
    assert r['total'] == 6
    assert [c[0] for c in r['cols']] == ['id', 'name', 'opened_at', 'tax_rate']
    assert len(r['sample']) == 3


def test_single_table_aggregate(dm_level5):
    r = dm_level5.query(
        'orders',
        'SELECT count(*) AS n, sum(order_total) AS total FROM orders',
        path='mcp',
    )
    assert r['cols'] == ['n', 'total']
    assert r['rows'][0][0] == 66375
    assert r['more'] is False


def test_paging(dm_level5):
    sql = 'SELECT id FROM customers ORDER BY id'
    p1 = dm_level5.query('customers', sql, path='a2a')
    p6 = dm_level5.query('customers', sql, page=6, path='a2a')
    assert p1['n'] == 500 and p1['more'] is True and p1['total'] == 2583
    assert p6['n'] == 83 and p6['more'] is False
    assert p1['rows'][0] != p6['rows'][0]


@pytest.mark.parametrize(
    'source,sql,msg',
    [
        (
            'orders',
            'SELECT * FROM orders o JOIN stores s ON o.store_id = s.id',
            'JOIN',
        ),
        ('orders', 'WITH x AS (SELECT * FROM orders) SELECT * FROM x', 'CTE'),
        (
            'orders',
            'SELECT id FROM orders UNION SELECT id FROM stores',
            'UNION',
        ),
        (
            'orders',
            'SELECT * FROM orders WHERE store_id IN (SELECT id FROM stores)',
            'only serves',
        ),
        ('orders', 'SELECT * FROM stores', 'only serves'),
        ('orders', 'DELETE FROM orders', 'only SELECT'),
        ('orders', 'DROP TABLE orders', 'only SELECT'),
        ('orders', 'SELECT 1; SELECT 2', 'exactly one'),
        ('orders', "SELECT * FROM read_csv('/etc/passwd')", 'not allowed'),
        ('orders', "SELECT * FROM '/etc/passwd'", 'only serves'),
        ('orders', 'SELECT 1', 'must read FROM'),
        ('nope', 'SELECT 1', 'unknown source'),
    ],
)
def test_rejections(dm_level5, source, sql, msg):
    r = dm_level5.query(source, sql, path='mcp')
    assert 'error' in r, r
    assert msg in r['error']


def test_duckdb_lock_blocks_external_access_even_without_validation(dm_level5):
    """Layer 2: bypass sqlglot and hit DuckDB directly. It must still refuse."""
    import duckdb

    cur = dm_level5._con.cursor()
    for sql in [
        "SELECT * FROM read_csv('/etc/passwd')",
        "COPY orders TO '/tmp/leak.csv'",
        'SET enable_external_access = true',
        'INSTALL httpfs',
        'CREATE TABLE x AS SELECT 1',
    ]:
        with pytest.raises(duckdb.Error):
            cur.execute(sql)


def test_every_call_is_recorded(dm_level1, recorder):
    before = len(recorder.events('manual'))
    dm_level1.query('orders', 'SELECT count(*) FROM orders', path='mcp')
    dm_level1.query(
        'orders', 'SELECT * FROM stores', path='mcp'
    )  # rejected, still counted
    events = recorder.events('manual')[before:]
    assert [e['ok'] for e in events] == [True, False]
    assert all(e['kind'] == 'dm_call' and e['path'] == 'mcp' for e in events)


def test_paging_is_consistent_without_order_by(dm_level5):
    """GROUP BY / DISTINCT without ORDER BY must still page without overlap or gaps."""
    sql = "SELECT DISTINCT customer FROM orders"
    seen, page = [], 1
    while True:
        r = dm_level5.query("orders", sql, page=page, path="a2a")
        seen.extend(row[0] for row in r["rows"])
        if not r["more"]:
            break
        page += 1
    assert len(seen) == len(set(seen)) == 2551  # all distinct customers, no duplicates


def test_each_table_has_its_own_isolated_duckdb_warehouse(dm_level5):
    """Each MCP source connects to its own separate <table_name>.duckdb file."""
    import duckdb

    assert len(dm_level5._db_paths) == 5
    for table, db_path in dm_level5._db_paths.items():
        assert db_path.name == f'{table}.duckdb'
        # Verify the other 4 tables physically do not exist inside this DuckDB file
        tables_in_db = [
            r[0] for r in dm_level5._cons[table].execute('SHOW TABLES').fetchall()
        ]
        assert tables_in_db == [table]
        for other in dm_level5.tables:
            if other != table:
                with pytest.raises(duckdb.Error):
                    dm_level5._cons[table].execute(
                        f'SELECT count(*) FROM "{other}"'
                    )

