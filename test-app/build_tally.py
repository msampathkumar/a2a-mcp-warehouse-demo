"""Build the public question list and the private answer key.

    python test-app/build_tally.py

Reads  test-app/questions.yaml + demo-app/data/*.csv
Writes demo-app/tally.json   (id, level, question, sources_needed, difficulty, answer_type)
       test-app/answers.json (id -> expected answer, computed with DuckDB JOINs)
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / 'test-app' / 'questions.yaml'
DATA = ROOT / 'demo-app' / 'data'
TALLY = ROOT / 'demo-app' / 'tally.json'
ANSWERS = ROOT / 'test-app' / 'answers.json'
TABLES = ['orders', 'stores', 'customers', 'order_items', 'products']
PUBLIC_KEYS = [
    'id',
    'level',
    'difficulty',
    'sources_needed',
    'question',
    'answer_type',
]


def _norm(v):
    if hasattr(v, 'is_integer') and isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if hasattr(v, '__int__') and type(v).__name__ in {'Decimal'}:
        return float(v)
    return v


def compute(q: dict, con: duckdb.DuckDBPyConnection):
    rows = con.execute(q['reference_sql']).fetchall()
    kind = q['answer_type']
    if q.get('argmax'):
        if len(rows) > 1 and rows[0][1] == rows[1][1]:
            raise ValueError(
                f'{q["id"]}: tie for first place {rows[:2]}; reword the question'
            )
        return _norm(rows[0][0])
    if kind in {'number', 'string'}:
        if len(rows) != 1 or len(rows[0]) != 1:
            raise ValueError(f'{q["id"]}: expected one value, got {rows[:3]}')
        return _norm(rows[0][0])
    if kind in {'list', 'set'}:
        return [_norm(r[0]) for r in rows]
    raise ValueError(f'{q["id"]}: unknown answer_type {kind}')


def main() -> int:
    spec = yaml.safe_load(QUESTIONS.read_text())
    con = duckdb.connect()
    for t in TABLES:
        con.execute(
            f"CREATE VIEW {t} AS SELECT * FROM read_csv_auto('{DATA / (t + '.csv')}')"
        )
    public, answers, seen = [], {}, set()
    for q in spec['questions']:
        if q['id'] in seen:
            raise ValueError(f'duplicate id {q["id"]}')
        seen.add(q['id'])
        bad = set(q['sources_needed']) - set(TABLES[: q['level']])
        if bad:
            raise ValueError(
                f'{q["id"]}: needs {bad}, not available at level {q["level"]}'
            )
        answers[q['id']] = {
            'answer_type': q['answer_type'],
            'expected': compute(q, con),
        }
        public.append({k: q[k] for k in PUBLIC_KEYS})
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    TALLY.write_text(
        json.dumps({'generated_at': stamp, 'questions': public}, indent=2)
        + '\n'
    )
    ANSWERS.write_text(
        json.dumps({'generated_at': stamp, 'answers': answers}, indent=2) + '\n'
    )
    for qid, a in answers.items():
        print(f'{qid}: {a["expected"]}')
    print(
        f'wrote {TALLY.relative_to(ROOT)} ({len(public)} questions) and {ANSWERS.relative_to(ROOT)}'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
