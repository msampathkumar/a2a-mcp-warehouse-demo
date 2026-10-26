"""DataManager: the only component that touches the data.

Rules (enforced here, for both MCP and A2A callers):
  * read-only;
  * one table per query: a single SELECT over exactly one source, no JOIN,
    no CTE, no UNION, no subquery over another table;
  * results paged at ROW_CAP rows, returned as compact JSON;
  * a fixed SOURCE_LATENCY_MS per call simulates data living in different places.

Two layers of protection:
  1. sqlglot validation -> friendly, structured errors.
  2. DuckDB itself is locked: the database file is opened read_only, external
     access (files, http, extensions) is disabled and the configuration is
     locked, so a query that slips past (1) still cannot read or write
     anything outside the loaded tables.
"""

from __future__ import annotations

import datetime as dt
import decimal
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import sqlglot
from sqlglot import exp
from opentelemetry import trace

from .telemetry import Recorder, tracer

# Functions that could reach outside the loaded tables. DuckDB's lock already
# blocks them; this list only turns them into a clear error message.
_BLOCKED_FUNCTION_PREFIXES = (
    'read_',
    'glob',
    'sniff_',
    'parquet_',
    'duckdb_',
    'pragma_',
    'query',
)


_SNAPSHOT_SLOTS = 64  # recent query results kept for consistent paging


class QueryRejected(ValueError):
    """The SQL broke a DataManager rule. Message is safe to show the caller."""


@dataclass
class _Validated:
    sql: str  # normalized SQL, used for cache keys


class DataManager:
    def __init__(
        self,
        data_dir: Path,
        tables: list[str],
        recorder: Recorder,
        row_cap: int = 500,
        source_latency_ms: int = 100,
        isolated_warehouses: bool = True,
    ) -> None:
        self.tables = list(tables)
        self.row_cap = row_cap
        self.source_latency_ms = source_latency_ms
        self.recorder = recorder
        self.isolated_warehouses = isolated_warehouses
        self._db_dir = Path(tempfile.mkdtemp(prefix='warehouse_'))
        self._db_paths: dict[str, Path] = {}
        self._cons: dict[str, duckdb.DuckDBPyConnection] = {}
        if isolated_warehouses:
            for t in self.tables:
                p = self._build_single_db(self._db_dir, data_dir, t)
                self._db_paths[t] = p
                c = duckdb.connect(
                    str(p),
                    read_only=True,
                    config={'enable_external_access': False, 'threads': 2},
                )
                c.execute('SET lock_configuration = true')
                self._cons[t] = c
            self._db_path = self._db_paths[self.tables[0]]
            self._con = self._cons[self.tables[0]]
        else:
            self._db_path = self._build_db(data_dir, self.tables)
            self._con = duckdb.connect(
                str(self._db_path),
                read_only=True,
                config={'enable_external_access': False, 'threads': 2},
            )
            self._con.execute('SET lock_configuration = true')
            for t in self.tables:
                self._db_paths[t] = self._db_path
                self._cons[t] = self._con
        self._schemas = {t: self._describe_columns(t) for t in self.tables}
        self._snapshots: OrderedDict[str, tuple[list[str], list[list[Any]]]] = OrderedDict()
        self._snap_lock = threading.Lock()
        self._row_counts = {
            t: self._cons[t].execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            for t in self.tables
        }

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _build_single_db(db_dir: Path, data_dir: Path, table: str) -> Path:
        """Build an isolated single-table DuckDB warehouse file (<table_name>.duckdb)."""
        csv = data_dir / f'{table}.csv'
        if not csv.exists():
            raise FileNotFoundError(csv)
        out = db_dir / f'{table}.duckdb'
        con = duckdb.connect(str(out))
        con.execute(
            f'CREATE TABLE "{table}" AS SELECT * FROM read_csv_auto(\'{csv}\')'
        )
        con.close()
        return out

    @staticmethod
    def _build_db(data_dir: Path, tables: list[str]) -> Path:
        """Copy only this level's CSVs into a private DuckDB file."""
        tmp = Path(tempfile.mkdtemp(prefix='warehouse_')) / 'warehouse.duckdb'
        con = duckdb.connect(str(tmp))
        for t in tables:
            csv = data_dir / f'{t}.csv'
            if not csv.exists():
                raise FileNotFoundError(csv)
            con.execute(
                f'CREATE TABLE "{t}" AS SELECT * FROM read_csv_auto(\'{csv}\')'
            )
        con.close()
        return tmp

    def _describe_columns(self, table: str) -> list[list[str]]:
        rows = self._cons[table].execute(f'DESCRIBE "{table}"').fetchall()
        return [[r[0], r[1]] for r in rows]

    # ------------------------------------------------------------ validation
    def validate(self, source: str, sql: str) -> _Validated:
        if source not in self.tables:
            raise QueryRejected(
                f"unknown source '{source}'. Available: {self.tables}"
            )
        try:
            statements = sqlglot.parse(sql, read='duckdb')
        except sqlglot.errors.ParseError as e:
            raise QueryRejected(
                f'SQL parse error: {str(e).splitlines()[0]}'
            ) from None
        statements = [s for s in statements if s is not None]
        if len(statements) != 1:
            raise QueryRejected('exactly one SQL statement is allowed')
        tree = statements[0]
        if isinstance(tree, (exp.Union, exp.Intersect, exp.Except)):
            raise QueryRejected('UNION/INTERSECT/EXCEPT are not allowed')
        if not isinstance(tree, exp.Select):
            raise QueryRejected('only SELECT statements are allowed')
        if tree.find(exp.Join):
            raise QueryRejected(
                'JOIN is not allowed: query one source at a time and combine results yourself'
            )
        if tree.find(exp.With) or tree.find(exp.CTE):
            raise QueryRejected('CTEs (WITH) are not allowed')
        if (
            tree.find(exp.Union)
            or tree.find(exp.Intersect)
            or tree.find(exp.Except)
        ):
            raise QueryRejected('UNION/INTERSECT/EXCEPT are not allowed')
        for fn in tree.find_all(exp.Func):
            name = (
                fn.sql_name() if not isinstance(fn, exp.Anonymous) else fn.name
            ).lower()
            if name.startswith(_BLOCKED_FUNCTION_PREFIXES):
                raise QueryRejected(f"function '{name}' is not allowed")
        names = {t.name.lower() for t in tree.find_all(exp.Table)}
        if not names:
            raise QueryRejected(f'query must read FROM {source}')
        if names != {source}:
            raise QueryRejected(
                f"this source only serves table '{source}'; query referenced {sorted(names)}"
            )
        return _Validated(sql=tree.sql(dialect='duckdb'))

    # ------------------------------------------------------------ operations
    def describe(self, source: str, *, path: str) -> dict:
        """Schema, row count and 3 sample rows for one source."""
        with tracer.start_as_current_span('data_manager.describe') as span:
            started = time.perf_counter()
            span.set_attributes(
                {'dm.source': source, 'dm.path': path, 'dm.op': 'describe'}
            )
            ok, err, result = True, None, {}
            try:
                if source not in self.tables:
                    raise QueryRejected(
                        f"unknown source '{source}'. Available: {self.tables}"
                    )
                self._simulate_distance()
                cur = self._cons[source].cursor()
                sample = cur.execute(
                    f'SELECT * FROM "{source}" LIMIT 3'
                ).fetchall()
                result = {
                    't': source,
                    'cols': self._schemas[source],
                    'total': self._row_counts[source],
                    'sample': [[_cell(v) for v in r] for r in sample],
                    'note': 'money columns are integer cents'
                    if source in {'orders', 'products'}
                    else None,
                }
                result = {k: v for k, v in result.items() if v is not None}
            except QueryRejected as e:
                ok, err = False, str(e)
                result = {'error': err}
            ms = round((time.perf_counter() - started) * 1000, 2)
            result['ms'] = ms
            self._record(
                path,
                'describe',
                source,
                None,
                ok,
                err,
                rows=len(result.get('sample', [])),
                ms=ms,
            )
            return result

    def query(self, source: str, sql: str, page: int = 1, *, path: str) -> dict:
        """Run one validated single-table SELECT and return one page."""
        with tracer.start_as_current_span('data_manager.query') as span:
            started = time.perf_counter()
            span.set_attributes(
                {
                    'dm.source': source,
                    'dm.path': path,
                    'dm.op': 'query',
                    'dm.page': page,
                }
            )
            ok, err, result, norm = True, None, {}, None
            try:
                page = max(1, int(page))
                v = self.validate(source, sql)
                norm = v.sql
                self._simulate_distance()
                cols, all_rows = self._snapshot(source, v.sql)
                total = len(all_rows)
                offset = (page - 1) * self.row_cap
                rows = all_rows[offset : offset + self.row_cap]
                result = {
                    't': source,
                    'cols': cols,
                    'rows': rows,
                    'n': len(rows),
                    'total': total,
                    'page': page,
                    'more': offset + len(rows) < total,
                }
            except QueryRejected as e:
                ok, err = False, str(e)
                result = {'error': err}
            except duckdb.Error as e:
                ok, err = False, f'query failed: {str(e).splitlines()[0]}'
                result = {'error': err}
            ms = round((time.perf_counter() - started) * 1000, 2)
            result['ms'] = ms
            span.set_attribute('dm.ok', ok)
            span.set_attribute('dm.rows', result.get('n', 0))
            if not ok:
                span.set_status(trace.Status(trace.StatusCode.ERROR, err))
            self._record(
                path,
                'query',
                source,
                norm or sql,
                ok,
                err,
                rows=result.get('n', 0),
                ms=ms,
                page=page,
            )
            return result

    # --------------------------------------------------------------- helpers
    def _snapshot(
        self, source: str, sql: str
    ) -> tuple[list[str], list[list[Any]]]:
        """Run the query once on `source`'s isolated warehouse and page from that result.

        Without ORDER BY, DuckDB may return GROUP BY / DISTINCT rows in a
        different order on every execution, so LIMIT/OFFSET pages could overlap
        or skip rows. A per-SQL snapshot keeps pages 1..N consistent. Data is
        read-only, so snapshots never go stale. Every page request still counts
        as one DataManager call and pays the simulated latency.
        """
        key = f'{source}:{sql}'
        with self._snap_lock:
            hit = self._snapshots.get(key)
            if hit is not None:
                self._snapshots.move_to_end(key)
                return hit
        rel = self._cons[source].cursor().execute(sql)
        cols = [d[0] for d in rel.description]
        rows = [[_cell(c) for c in r] for r in rel.fetchall()]
        with self._snap_lock:
            self._snapshots[key] = (cols, rows)
            while len(self._snapshots) > _SNAPSHOT_SLOTS:
                self._snapshots.popitem(last=False)
        return cols, rows

    def _simulate_distance(self) -> None:
        if self.source_latency_ms > 0:
            time.sleep(self.source_latency_ms / 1000)

    def _record(
        self, path, op, source, sql, ok, err, *, rows, ms, page=None
    ) -> None:
        self.recorder.record(
            'dm_call',
            path=path,
            op=op,
            source=source,
            sql=sql,
            page=page,
            ok=ok,
            error=err,
            rows=rows,
            ms=ms,
        )

    def schemas(self) -> dict[str, Any]:
        return {
            t: {'cols': self._schemas[t], 'total': self._row_counts[t]}
            for t in self.tables
        }


def _cell(v: Any) -> Any:
    """Make DuckDB values JSON-friendly and compact."""
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, float):
        return round(v, 6)
    return v
