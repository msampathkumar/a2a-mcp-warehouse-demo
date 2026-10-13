"""Client-side cache + workspace for the MCP path (mirror of the A2A agent's).

The MCP-path client agent must combine sources itself. It gets the same
helpers the A2A agent has server-side: save_result (fetch all pages of one
source query), list_workspace, local_sql (DuckDB JOINs over saved frames),
plus a TTL cache in front of MCP tool calls.
"""

from __future__ import annotations

import json
import re
import time
from typing import Awaitable, Callable

import duckdb
import pandas as pd

MAX_FETCH_ROWS = 20_000  # same cap as the A2A agent's workspace
LOCAL_RESULT_ROWS = 200
_SAFE_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,40}$')

# (source, sql, page) -> parsed DataManager JSON
Fetcher = Callable[[str, str, int], Awaitable[dict]]


class ClientWorkspace:
    def __init__(
        self,
        cache_enabled: bool,
        cache_ttl_min: float,
        on_cache_hit: Callable[[str, str], None],
    ) -> None:
        self.cache_active = cache_enabled and cache_ttl_min > 0
        self.ttl_s = cache_ttl_min * 60
        self.on_cache_hit = on_cache_hit
        self.queries: dict[tuple, tuple[float, dict]] = {}
        self.frames: dict[str, tuple[float, pd.DataFrame, dict]] = {}

    def reset(self) -> None:
        self.queries.clear()
        self.frames.clear()

    def new_question(self) -> None:
        """Cache off: nothing survives between questions."""
        if not self.cache_active:
            self.reset()

    def _fresh(self, ts: float) -> bool:
        return self.cache_active and time.time() - ts < self.ttl_s

    def _live(self, ts: float) -> bool:
        return self._fresh(ts) if self.cache_active else True

    # ------------------------------------------------------------ MCP cache
    @staticmethod
    def key(tool: str, args: dict) -> tuple:
        sql = ' '.join(str(args.get('sql', '')).lower().split())
        return (tool, sql, int(args.get('page', 1) or 1))

    def cached(self, tool: str, args: dict) -> dict | None:
        hit = self.queries.get(self.key(tool, args))
        if hit and self._fresh(hit[0]):
            self.on_cache_hit(tool, 'mcp_tool')
            return hit[1]
        return None

    def store(self, tool: str, args: dict, response: dict) -> None:
        if self.cache_active and 'error' not in json.dumps(response)[:200]:
            self.queries[self.key(tool, args)] = (time.time(), response)

    # ------------------------------------------------------------ workspace
    async def save_result(
        self, name: str, source: str, sql: str, fetch: Fetcher
    ) -> dict:
        if not _SAFE_NAME.match(name):
            return {
                'error': 'name must be a simple identifier, e.g. orders_by_store'
            }
        held = self.frames.get(name)
        if (
            held
            and self._fresh(held[0])
            and held[2] == {'source': source, 'sql': sql}
        ):
            self.on_cache_hit(f'query_{source}', 'save_result')
            return {**self._summary(name, held[1]), 'cached': True}
        rows, cols, page, pages = [], None, 1, 0
        while True:
            r = await fetch(source, sql, page)
            if 'error' in r:
                return r
            cols, pages = r['cols'], pages + 1
            rows.extend(r['rows'])
            if len(rows) > MAX_FETCH_ROWS:
                return {
                    'error': f'more than {MAX_FETCH_ROWS} rows; aggregate or filter in SQL first'
                }
            if not r.get('more'):
                break
            page += 1
        df = pd.DataFrame(rows, columns=cols)
        self.frames[name] = (time.time(), df, {'source': source, 'sql': sql})
        return {**self._summary(name, df), 'pages': pages}

    def list_workspace(self) -> dict:
        return {
            'frames': [
                {'name': n, **meta, 'rows': len(df), 'cols': list(df.columns)}
                for n, (ts, df, meta) in self.frames.items()
                if self._live(ts)
            ]
        }

    def local_sql(self, sql: str) -> dict:
        frames = {
            n: df for n, (ts, df, _) in self.frames.items() if self._live(ts)
        }
        con = duckdb.connect()
        try:
            for n, df in frames.items():
                con.register(n, df)
            con.execute('SET enable_external_access = false')
            con.execute('SET lock_configuration = true')
            rel = con.execute(sql)
            cols = [d[0] for d in rel.description]
            rows = rel.fetchmany(LOCAL_RESULT_ROWS + 1)
            return {
                'cols': cols,
                'rows': [
                    [_cell(v) for v in r] for r in rows[:LOCAL_RESULT_ROWS]
                ],
                'truncated': len(rows) > LOCAL_RESULT_ROWS,
            }
        except duckdb.Error as e:
            return {
                'error': f'local_sql failed: {str(e).splitlines()[0]}',
                'frames': list(frames),
            }
        finally:
            con.close()

    @staticmethod
    def _summary(name: str, df: pd.DataFrame) -> dict:
        return {
            'name': name,
            'rows': len(df),
            'cols': list(df.columns),
            'preview': json.loads(
                df.head(5).to_json(orient='values', date_format='iso')
            ),
        }


def _cell(v):
    if hasattr(v, 'isoformat'):
        return v.isoformat()
    if isinstance(v, float):
        return round(v, 6)
    return v
