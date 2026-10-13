"""Per-run cache + workspace used by the A2A agent.

Scope: one entry per run_id (from W3C baggage; 'manual' otherwise).
  * query cache   (source, normalized SQL, page) -> DataManager result
  * frames        name -> pandas DataFrame pulled from a source
  * notes         /tmp/a2a_cache/<run_id>/notes.md (schema + join facts)
Everything expires after CACHE_TTL_MIN; CACHE_ENABLED=false or TTL 0 disables
reuse. The harness calls POST /_reset at the start of every trial.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pandas as pd

from .config import Settings
from .data_manager import DataManager, QueryRejected
from .telemetry import Recorder, run_attrs

MAX_FETCH_ROWS = 20_000  # same cap as the MCP client's save_result
LOCAL_RESULT_ROWS = 200
_SAFE_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,40}$')


@dataclass
class _Scope:
    queries: dict[tuple, tuple[float, dict]] = field(default_factory=dict)
    frames: dict[str, tuple[float, pd.DataFrame, dict]] = field(
        default_factory=dict
    )


class Workspace:
    def __init__(
        self,
        settings: Settings,
        dm: DataManager,
        recorder: Recorder,
        path: str = 'a2a',
    ) -> None:
        self.s = settings
        self.dm = dm
        self.recorder = recorder
        self.path = path
        self._lock = threading.Lock()
        self._scopes: dict[str, _Scope] = {}

    # ----------------------------------------------------------------- scope
    def _run_id(self) -> str:
        return run_attrs()['run_id']

    def _scope_key(self) -> str:
        """Cache on: share across a run's questions. Cache off: one question only."""
        a = run_attrs()
        return (
            a['run_id']
            if self.s.cache_active
            else f'{a["run_id"]}::{a["q_id"]}'
        )

    def _scope(self) -> _Scope:
        with self._lock:
            return self._scopes.setdefault(self._scope_key(), _Scope())

    def _fresh(self, ts: float) -> bool:
        """Is a cached entry reusable? Always False when caching is off."""
        return (
            self.s.cache_active
            and (time.time() - ts) < self.s.cache_ttl_min * 60
        )

    def _live(self, ts: float) -> bool:
        """Is a workspace frame still visible? Frames are working memory: with
        caching off they live for the current question's scope only."""
        return self._fresh(ts) if self.s.cache_active else True

    def reset(self, run_id: str | None = None) -> list[str]:
        """Drop cache, frames and notes for one run (or all)."""
        with self._lock:
            keys = [
                k
                for k in self._scopes
                if run_id is None or k == run_id or k.startswith(f'{run_id}::')
            ]
            for k in keys:
                self._scopes.pop(k, None)
        ids = [run_id] if run_id else sorted({k.split('::')[0] for k in keys})
        for rid in ids:
            shutil.rmtree(self._notes_dir(rid), ignore_errors=True)
        return ids

    # ------------------------------------------------------------ data calls
    def describe(self, source: str) -> dict:
        return self._cached(
            ('describe', source),
            lambda: self.dm.describe(source, path=self.path),
            source,
            'describe',
        )

    def query(self, source: str, sql: str, page: int = 1) -> dict:
        try:
            norm = self.dm.validate(source, sql).sql
        except QueryRejected:
            norm = sql  # let DataManager reject (and count) it
        return self._cached(
            ('query', source, norm, int(page)),
            lambda: self.dm.query(source, sql, page, path=self.path),
            source,
            'query',
        )

    def _cached(self, key: tuple, call, source: str, op: str) -> dict:
        scope = self._scope()
        hit = scope.queries.get(key)
        if hit and self._fresh(hit[0]):
            self.recorder.record(
                'cache_hit', path=self.path, op=op, source=source
            )
            return {**hit[1], 'cached': True}
        result = call()
        if 'error' not in result and self.s.cache_active:
            scope.queries[key] = (time.time(), result)
        return result

    # -------------------------------------------------------------- workspace
    def save_result(self, name: str, source: str, sql: str) -> dict:
        if not _SAFE_NAME.match(name):
            return {
                'error': 'name must be a simple identifier, e.g. orders_by_store'
            }
        scope = self._scope()
        held = scope.frames.get(name)
        if (
            held
            and self._fresh(held[0])
            and held[2]['source'] == source
            and held[2]['sql'] == sql
        ):
            self.recorder.record(
                'cache_hit', path=self.path, op='save_result', source=source
            )
            return {**self._frame_summary(name, held[1]), 'cached': True}
        pages, rows, cols, page = 0, [], None, 1
        while True:
            r = self.query(source, sql, page)
            if 'error' in r:
                return r
            cols, pages = r['cols'], pages + 1
            rows.extend(r['rows'])
            if len(rows) > MAX_FETCH_ROWS:
                return {
                    'error': f'more than {MAX_FETCH_ROWS} rows; aggregate or filter in SQL first'
                }
            if not r['more']:
                break
            page += 1
        df = pd.DataFrame(rows, columns=cols)
        scope.frames[name] = (time.time(), df, {'source': source, 'sql': sql})
        return {**self._frame_summary(name, df), 'pages': pages}

    def semi_join_fetch(
        self, name: str, source: str, columns: str, key_column: str,
        frame: str, frame_column: str, extra_where: str = "",
    ) -> dict:
        """Fetch rows of `source` whose key_column is IN frame.frame_column, in batches."""
        if not _SAFE_NAME.match(name):
            return {"error": "name must be a simple identifier"}
        scope = self._scope()
        held = scope.frames.get(frame)
        if not held:
            return {"error": f"frame '{frame}' not in workspace"}
        df = held[1]
        if frame_column not in df.columns:
            return {"error": f"column '{frame_column}' not in frame '{frame}'"}
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_column):
            return {"error": "key_column must be a column name"}
        values = sorted({str(v) for v in df[frame_column].dropna()})
        if not values:
            return {"error": "no key values in frame"}
        frames, pages = [], 0
        for i in range(0, len(values), 200):
            chunk = values[i : i + 200]
            in_list = ", ".join("'" + v.replace("'", "''") + "'" for v in chunk)
            where = f"{key_column} IN ({in_list})" + (f" AND ({extra_where})" if extra_where else "")
            sql = f"SELECT {columns} FROM {source} WHERE {where}"
            page = 1
            while True:
                r = self.query(source, sql, page)
                if "error" in r:
                    return r
                pages += 1
                frames.append(pd.DataFrame(r["rows"], columns=r["cols"]))
                if not r["more"]:
                    break
                page += 1
            if sum(len(f) for f in frames) > MAX_FETCH_ROWS:
                return {"error": f"more than {MAX_FETCH_ROWS} rows; filter the frame first"}
        out = pd.concat(frames, ignore_index=True)
        scope.frames[name] = (time.time(), out, {"source": source, "sql": f"semi_join {key_column} IN {frame}.{frame_column}"})
        return {**self._frame_summary(name, out), "pages": pages, "keys": len(values)}

    def list_workspace(self) -> dict:
        scope = self._scope()
        return {
            'frames': [
                {
                    'name': n,
                    'source': meta['source'],
                    'sql': meta['sql'],
                    'rows': len(df),
                    'cols': list(df.columns),
                }
                for n, (ts, df, meta) in scope.frames.items()
                if self._live(ts)
            ]
        }

    def local_sql(self, sql: str) -> dict:
        """DuckDB over saved frames only. Joins allowed here; no files, no network."""
        scope = self._scope()
        frames = {
            n: df for n, (ts, df, _) in scope.frames.items() if self._live(ts)
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
    def _frame_summary(name: str, df: pd.DataFrame) -> dict:
        return {
            'name': name,
            'rows': len(df),
            'cols': list(df.columns),
            'preview': json.loads(
                df.head(5).to_json(orient='values', date_format='iso')
            ),
        }

    # ----------------------------------------------------------------- notes
    def _notes_dir(self, run_id: str) -> Path:
        base = self.s.cache_dir.resolve()
        safe = re.sub(r'[^A-Za-z0-9_.-]', '_', run_id)[:80] or 'manual'
        target = (base / safe).resolve()
        if base not in target.parents:
            raise PermissionError('path escapes cache dir')
        return target

    def _notes_file(self) -> Path:
        d = self._notes_dir(self._run_id())
        d.mkdir(parents=True, exist_ok=True)
        f = d / 'notes.md'
        if f.is_symlink():
            raise PermissionError('symlinks are not allowed')
        return f

    def read_notes(self) -> dict:
        if not self.s.notes_enabled:
            return {'notes': '', 'disabled': True}
        f = self._notes_file()
        return {'notes': f.read_text(encoding='utf-8') if f.exists() else ''}

    def write_notes(self, text: str) -> dict:
        if not self.s.notes_enabled:
            return {'ok': False, 'disabled': True}
        f = self._notes_file()
        text = text[:4000]
        f.write_text(text, encoding='utf-8')
        return {'ok': True, 'chars': len(text)}


def _cell(v):
    if hasattr(v, 'isoformat'):
        return v.isoformat()
    if isinstance(v, float):
        return round(v, 6)
    return v
